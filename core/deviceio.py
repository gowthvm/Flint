"""Low-level Win32 helpers for raw disk I/O.

Single source of truth for ctypes kernel32 wiring, constants, and
device primitives used by the write, verify, wipe, backup, benchmark,
and bootcheck modules.
"""

import ctypes
import time
from collections.abc import Callable
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

FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

TRANSIENT_ERRORS = frozenset({1117, 21, 31, 5, 1167})
TRANSIENT_SEEK_ERRORS = frozenset({21, 31, 5, 1167})


class _Cancelled(Exception):
    """Raised by ``read_bytes_retry`` when the cancel callback fires."""


def _configure_kernel32() -> Any:
    """Configure ctypes argtypes/restype once at import time."""
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


_K32 = _configure_kernel32()


def kernel32() -> Any:
    return _K32


def open_drive(path: str, *, write: bool, flags: int = 0) -> Any:
    """Open a raw disk (or volume) handle; raise OSError when it fails.

    Parameters
    ----------
    path:
        Device path such as ``\\\\.\\E:`` or ``\\\\.\\PHYSICALDRIVE0``.
    write:
        Open for write access (``GENERIC_WRITE``).
    flags:
        Additional ``CreateFileW`` flags.  Pass
        ``FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH`` for direct
        sector-aligned I/O.
    """
    k32 = kernel32()
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    handle = k32.CreateFileW(
        path,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        err = k32.GetLastError()
        if write and err == 5:  # ERROR_ACCESS_DENIED
            raise OSError(
                f"access denied: {path} -- run Flint as "
                "administrator to write to raw drives"
            )
        raise OSError(
            f"could not open {path} for {'write' if write else 'read'} (error {err})"
        )
    return handle


def drive_size(handle: Any) -> int:
    """Return the drive capacity in bytes; raise OSError on failure."""
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


def seek(handle: Any, offset: int) -> None:
    """Position *handle* at *offset*; raise OSError on failure."""
    position = ctypes.c_longlong()
    if not kernel32().SetFilePointerEx(
        handle, ctypes.c_longlong(offset), ctypes.byref(position), 0
    ):
        raise OSError(f"failed to seek to byte {offset:,}")


def seek_retry(handle: Any, offset: int, retries: int = 3) -> bool:
    """Seek to *offset*, retrying on transient errors.

    Returns ``True`` on success, ``False`` if all attempts fail.
    """
    for _ in range(retries + 1):
        position = ctypes.c_longlong()
        if kernel32().SetFilePointerEx(
            handle, ctypes.c_longlong(offset), ctypes.byref(position), 0
        ):
            return True
        time.sleep(0.05)
    return False


def lock_volumes(letters: list[str]) -> list[Any]:
    """Dismount and lock every volume on a drive so its filesystem does not
    fight a raw read/write. Returns the held handles (unlock first).

    Raises OSError if any volume cannot be opened or locked.
    """
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
            unlock_volumes(held)
            raise OSError(
                f"Volume {letter}: could not be opened for locking."
            )
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
        raise OSError(f"read failed: {k32.GetLastError()}")
    return buffer.raw[: read.value]


def read_bytes_retry(
    handle: Any,
    count: int,
    retries: int = 3,
    is_cancelled: Callable[[], bool] | None = None,
) -> bytes | None:
    """Read up to *count* bytes, retrying on transient errors.

    Returns the bytes read (possibly fewer than *count* at end-of-device),
    ``None`` when every attempt fails, or raises ``_Cancelled`` when the
    cancel callback fires.
    """
    k32 = kernel32()
    for attempt in range(retries + 1):
        if is_cancelled is not None and is_cancelled():
            raise _Cancelled()
        buffer = ctypes.create_string_buffer(count)
        read = ctypes.c_ulong()
        if k32.ReadFile(handle, buffer, count, ctypes.byref(read), None):
            return buffer.raw[: read.value]
        if attempt < retries:
            time.sleep(0.05)
    return None


def write_bytes(handle: Any, data: bytes) -> None:
    """Write all of *data*; raise OSError on failure or short write."""
    k32 = kernel32()
    buffer = ctypes.create_string_buffer(data)
    written = ctypes.c_ulong()
    if not k32.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
        raise OSError(f"write failed: {k32.GetLastError()}")
    if written.value != len(data):
        raise OSError("short write on drive")


def write_bytes_retry(
    handle: Any,
    data: bytes,
    max_retries: int = 3,
    error_suffix: str = "",
) -> None:
    """Write *data* with automatic retries for transient errors and short writes."""
    k32 = kernel32()
    last_err = 0
    for attempt in range(max_retries + 1):
        buffer = ctypes.create_string_buffer(data)
        written = ctypes.c_ulong()
        ok = k32.WriteFile(
            handle,
            buffer,
            len(data),
            ctypes.byref(written),
            None,
        )
        if ok and written.value == len(data):
            return
        if ok and written.value < len(data):
            last_err = 0  # short write, no Win32 error
        else:
            last_err = k32.GetLastError()
        if attempt < max_retries and (
            last_err in TRANSIENT_ERRORS or (ok and written.value < len(data))
        ):
            time.sleep(0.5 * (2 ** attempt))
            continue
        break
    if last_err in TRANSIENT_ERRORS:
        raise OSError(
            f"write failed: {last_err} (USB device became unresponsive "
            f"after {max_retries} retries{error_suffix})"
        )
    if not ok:
        raise OSError(f"write failed: {last_err}")
    raise OSError("short write on drive")


def flush(handle: Any) -> None:
    """Flush write cache to physical media; raise OSError on failure."""
    if not kernel32().FlushFileBuffers(handle):
        raise OSError("flush failed: data may not have reached the drive")
