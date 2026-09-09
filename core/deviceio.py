"""Low-level Win32 helpers for raw disk I/O.

Single source of truth for ctypes kernel32 wiring, constants, and
device primitives used by the write, verify, wipe, backup, benchmark,
and bootcheck modules.
"""

import ctypes
import time
from typing import Any

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
FSCTL_DISMOUNT_VOLUME = 0x00090020
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

TRANSIENT_ERRORS = frozenset({1117, 21, 31, 5, 1167})
TRANSIENT_SEEK_ERRORS = frozenset({21, 31, 5, 1167})


def kernel32() -> Any:
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_ulong
    k32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    k32.DeviceIoControl.restype = ctypes.c_ulong
    k32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    k32.ReadFile.restype = ctypes.c_ulong
    k32.WriteFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    k32.WriteFile.restype = ctypes.c_ulong
    k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    k32.FlushFileBuffers.restype = ctypes.c_ulong
    k32.SetFilePointerEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        ctypes.c_ulong,
    ]
    k32.SetFilePointerEx.restype = ctypes.c_ulong
    k32.SetThreadExecutionState.argtypes = [ctypes.c_ulong]
    k32.SetThreadExecutionState.restype = ctypes.c_ulong
    k32.GetLastError.restype = ctypes.c_ulong
    return k32


def open_drive(path: str, *, write: bool) -> Any:
    """Open a raw disk (or volume) handle; raise OSError when it fails."""
    k32 = kernel32()
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    handle = k32.CreateFileW(
        path,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise OSError(f"could not open {path} for {'write' if write else 'read'}")
    return handle


def drive_size(handle: Any) -> int:
    k32 = kernel32()
    length = ctypes.c_ulonglong()
    returned = ctypes.c_ulong()
    ok = k32.DeviceIoControl(
        handle,
        IOCTL_DISK_GET_LENGTH_INFO,
        None,
        0,
        ctypes.byref(length),
        ctypes.sizeof(length),
        ctypes.byref(returned),
        None,
    )
    if not ok or length.value <= 0:
        raise OSError("could not determine drive size")
    return length.value


def _ioctl(handle: Any, code: int) -> bool:
    returned = ctypes.c_ulong()
    return bool(
        kernel32().DeviceIoControl(
            handle,
            code,
            None,
            0,
            None,
            0,
            ctypes.byref(returned),
            None,
        )
    )


def lock_volumes(letters: list[str]) -> list[Any]:
    """Dismount and lock every volume on a drive so its filesystem does not
    fight a raw read/write. Returns the held handles (unlock first)."""
    held: list[Any] = []
    for letter in letters:
        handle = kernel32().CreateFileW(
            f"\\\\.\\{letter}:",
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            continue
        _ioctl(handle, FSCTL_DISMOUNT_VOLUME)
        locked = False
        for _ in range(5):
            if _ioctl(handle, FSCTL_LOCK_VOLUME):
                locked = True
                break
            time.sleep(0.2)
        if not locked:
            kernel32().CloseHandle(handle)
            unlock_volumes(held)
            raise OSError(
                f"Volume {letter}: is in use by another program. "
                "Close it and try again."
            )
        held.append(handle)
    return held


def unlock_volumes(held: list[Any]) -> None:
    k32 = kernel32()
    for handle in held:
        _ioctl(handle, FSCTL_UNLOCK_VOLUME)
        k32.CloseHandle(handle)


def read_bytes(handle: Any, count: int) -> bytes:
    """Read exactly up to ``count`` bytes; returns b"" at end of device,
    raises OSError on a failed read."""
    k32 = kernel32()
    buffer = ctypes.create_string_buffer(count)
    read = ctypes.c_ulong()
    if not k32.ReadFile(handle, buffer, count, ctypes.byref(read), None):
        raise OSError(f"read failed: {ctypes.windll.kernel32.GetLastError()}")
    return buffer.raw[: read.value]


def write_bytes(handle: Any, data: bytes) -> None:
    k32 = kernel32()
    buffer = ctypes.create_string_buffer(data)
    written = ctypes.c_ulong()
    if not k32.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
        raise OSError(f"write failed: {ctypes.windll.kernel32.GetLastError()}")
    if written.value != len(data):
        raise OSError("short write on drive")


def flush(handle: Any) -> None:
    if not kernel32().FlushFileBuffers(handle):
        raise OSError("flush failed: data may not have reached the drive")
