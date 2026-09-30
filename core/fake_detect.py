"""Detect counterfeit USB drives with inflated capacity reports.

The probe is **non-destructive**: it reads from high offsets near the end
of the reported capacity.  Failing reads (cannot open, seek/read errors,
short reads) mean the drive does not actually have the capacity it
advertises and are reported as suspicious.  Uniform data is reported as
*unverified* rather than fake, because a freshly formatted or never-written
region is uniform too - the caller may warn, but the flash is not blocked.

A destructive write-back probe (write a pattern to the tail, read it back)
used to live here; nothing called it and the only CLI flag named
``--verify`` refers to post-flash read-back verification, so it was removed
rather than left as dead, drive-erasing code.
"""

import ctypes
import logging
from typing import Any

from core.deviceio import (
    _INVALID_HANDLE_VALUE,
    GENERIC_READ,
    OPEN_EXISTING,
    kernel32,
)

logger = logging.getLogger("flint")

PROBE_SIZE = 1024 * 1024  # 1 MiB read probe
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
        raise OSError(kernel32().GetLastError(), f"cannot open {path} for reading")
    return handle


def _seek(handle: Any, offset: int) -> None:
    """Seek to *offset* in the drive handle."""
    new_pos = ctypes.c_longlong()
    ok = kernel32().SetFilePointerEx(
        handle, ctypes.c_longlong(offset), ctypes.byref(new_pos), 0
    )
    if not ok:
        raise OSError(kernel32().GetLastError(), "SetFilePointerEx failed")


def _read_chunk(handle: Any, size: int) -> bytes:
    """Read *size* bytes from the current position."""
    buf = ctypes.create_string_buffer(size)
    n_read = ctypes.c_ulong(0)
    ok = kernel32().ReadFile(handle, buf, size, ctypes.byref(n_read), None)
    if not ok:
        raise OSError(kernel32().GetLastError(), "ReadFile failed")
    return buf.raw[: n_read.value]


def _is_uniform(data: bytes) -> bool:
    """Return True if *data* is all the same byte or all zeros."""
    if len(data) < 2:
        return True
    first = data[0]
    return all(b == first for b in data)


def probe_capacity(drive_path: str, reported_bytes: int) -> tuple[bool, str]:
    """Non-destructive probe: try reading from the end of reported capacity.

    Returns ``(suspicious, message)``.  ``suspicious`` is True only for
    failures that prove the capacity is not there; uniform data comes back
    as ``False`` with an ``unverified`` message (see module docstring).
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
        return False, (
            f"unverified: read-back at offset {probe_offset:#x} is "
            f"{len(data):,} identical bytes - blank region or "
            f"counterfeit media, not treated as a failure"
        )
    return False, "non-destructive probe passed"
