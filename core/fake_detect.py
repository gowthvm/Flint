"""Detect counterfeit USB drives with inflated capacity reports.

Two detection strategies:

1. **Non-destructive probe** (default): reads from high offsets near the
   end of the reported capacity. If the reads fail or return suspiciously
   uniform data, the drive is flagged.

2. **Destructive write-back** (``--verify``): writes a known pattern to
   the last few megabytes of the reported capacity and reads it back.
   Mismatched data confirms a counterfeit drive. This erases data in
   the probe region.
"""

import ctypes
import hashlib
import logging
import struct
from typing import Any

from core.deviceio import (
    GENERIC_READ,
    GENERIC_WRITE,
    OPEN_EXISTING,
    kernel32,
)

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

logger = logging.getLogger("flint")

PROBE_SIZE = 1024 * 1024  # 1 MiB read probe
WRITE_BACK_SIZE = 256 * 1024  # 256 KiB write-back probe
_SECTOR_SIZE = 512


def _open_for_read(path: str) -> Any:
    """Open a drive handle for reading."""
    handle = kernel32().CreateFileW(
        path,
        GENERIC_READ,
        0,  # exclusive
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), f"cannot open {path} for reading")
    return handle


def _open_for_write(path: str) -> Any:
    """Open a drive handle for writing."""
    handle = kernel32().CreateFileW(
        path,
        GENERIC_WRITE,
        0,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), f"cannot open {path} for writing")
    return handle


def _seek(handle: Any, offset: int) -> None:
    """Seek to *offset* in the drive handle."""
    new_pos = ctypes.c_longlong()
    ok = kernel32().SetFilePointerEx(
        handle, ctypes.c_longlong(offset), ctypes.byref(new_pos), 0
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")


def _read_chunk(handle: Any, size: int) -> bytes:
    """Read *size* bytes from the current position."""
    buf = ctypes.create_string_buffer(size)
    n_read = ctypes.c_ulong(0)
    ok = kernel32().ReadFile(handle, buf, size, ctypes.byref(n_read), None)
    if not ok:
        raise OSError(ctypes.get_last_error(), "ReadFile failed")
    return buf.raw[: n_read.value]


def _write_chunk(handle: Any, data: bytes) -> int:
    """Write *data* and return bytes written."""
    buf = ctypes.create_string_buffer(data)
    n_written = ctypes.c_ulong(0)
    ok = kernel32().WriteFile(handle, buf, len(data), ctypes.byref(n_written), None)
    if not ok:
        raise OSError(ctypes.get_last_error(), "WriteFile failed")
    return n_written.value


def _is_uniform(data: bytes) -> bool:
    """Return True if *data* is all the same byte or all zeros."""
    if len(data) < 2:
        return True
    first = data[0]
    return all(b == first for b in data)


def probe_capacity(drive_path: str, reported_bytes: int) -> tuple[bool, str]:
    """Non-destructive probe: try reading from the end of reported capacity.

    Returns ``(suspicious, message)``.
    """
    probe_offset = max(0, reported_bytes - PROBE_SIZE)
    try:
        handle = _open_for_read(drive_path)
    except OSError as exc:
        return True, f"cannot open drive for probe: {exc}"
    try:
        _seek(handle, probe_offset)
        data = _read_chunk(handle, PROBE_SIZE)
    except OSError as exc:
        return True, f"read probe failed at offset {probe_offset:#x}: {exc}"
    finally:
        kernel32().CloseHandle(handle)

    if len(data) < PROBE_SIZE:
        return True, (
            f"short read at offset {probe_offset:#x}: "
            f"got {len(data):,} bytes, expected {PROBE_SIZE:,}"
        )
    if _is_uniform(data):
        return True, (
            f"suspect counterfeit: read-back at offset {probe_offset:#x} "
            f"is {len(data):,} identical bytes — real drives show mixed data"
        )
    return False, "non-destructive probe passed"


def write_back_verify(
    drive_path: str, reported_bytes: int
) -> tuple[bool, str]:
    """Destructive write-back test: write a pattern to the end and read it back.

    Returns ``(is_fake, message)``.  The probe region is erased.
    """
    write_offset = max(0, reported_bytes - WRITE_BACK_SIZE)
    # Deterministic pattern derived from the offset
    seed = hashlib.sha256(struct.pack("<Q", write_offset)).digest()
    pattern = (seed * (WRITE_BACK_SIZE // len(seed) + 1))[:WRITE_BACK_SIZE]

    try:
        handle = _open_for_write(drive_path)
    except OSError as exc:
        return True, f"cannot open drive for write-back: {exc}"
    try:
        _seek(handle, write_offset)
        written = _write_chunk(handle, pattern)
        if written < len(pattern):
            return True, (
                f"short write at offset {write_offset:#x}: "
                f"wrote {written:,} bytes, expected {len(pattern):,}"
            )
    except OSError as exc:
        return True, f"write-back failed at offset {write_offset:#x}: {exc}"
    finally:
        kernel32().CloseHandle(handle)

    # Read back and compare
    try:
        handle = _open_for_read(drive_path)
    except OSError as exc:
        return True, f"cannot open drive for read-back: {exc}"
    try:
        _seek(handle, write_offset)
        data = _read_chunk(handle, len(pattern))
    except OSError as exc:
        return True, f"read-back failed at offset {write_offset:#x}: {exc}"
    finally:
        kernel32().CloseHandle(handle)

    if data != pattern:
        return True, (
            f"CONFIRMED COUNTERFEIT: write-back pattern mismatch at "
            f"offset {write_offset:#x} — drive has less capacity than "
            f"reported ({reported_bytes:,} bytes)"
        )
    return False, "write-back verify passed"
