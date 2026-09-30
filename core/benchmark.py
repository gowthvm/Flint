"""Drive speed benchmark — measure sequential read/write throughput."""

import ctypes
import logging
import os
import time

from core.deviceio import (
    GENERIC_READ,
    GENERIC_WRITE,
    kernel32,
)

logger = logging.getLogger("flint")

DEFAULT_BENCH_SIZE = 64 * 1024 * 1024  # 64 MB
DEFAULT_CHUNK = 8 * 1024 * 1024  # 8 MB


def benchmark_write(
    drive_path: str,
    size: int = DEFAULT_BENCH_SIZE,
    chunk: int = DEFAULT_CHUNK,
    *,
    confirm: bool = False,
) -> float:
    """Write a test pattern to the drive and return speed in MB/s.

    .. warning::
        **DESTRUCTIVE.** Overwrites the first ``size`` bytes of
        ``drive_path`` (64 MB by default) with ``os.urandom`` — that
        destroys the partition table, filesystem and any data at the start
        of the device.  Callers must only reach this from a flow that has
        explicitly confirmed the destruction: the call raises
        ``ValueError`` unless ``confirm=True`` is passed.
    """
    if not confirm:
        raise ValueError(
            "benchmark_write would overwrite the first "
            f"{size:,} bytes of {drive_path} with random data, destroying "
            "its partition table and filesystem; pass confirm=True only "
            "after the user explicitly confirmed destroying that drive"
        )
    k32 = kernel32()
    INVALID = ctypes.c_void_p(-1).value

    handle = k32.CreateFileW(
        drive_path, GENERIC_WRITE, 0, None, 3, 0, None,
    )
    if not handle or handle == INVALID:
        return 0.0

    pattern = os.urandom(chunk)
    buf = ctypes.create_string_buffer(pattern)
    chunk_len = len(pattern)
    written = 0
    start = time.perf_counter()
    try:
        while written < size:
            # Never write past the size the caller confirmed: a non
            # multiple of `chunk` would otherwise overshoot by up to
            # chunk-1 bytes.
            count = min(chunk_len, size - written)
            n = ctypes.c_ulong()
            ok = k32.WriteFile(handle, buf, count, ctypes.byref(n), None)
            if not ok or n.value != count:
                break
            written += n.value
    finally:
        k32.CloseHandle(handle)

    elapsed = time.perf_counter() - start
    return written / elapsed / 1_000_000 if elapsed > 0 else 0.0


def benchmark_read(drive_path: str, size: int = DEFAULT_BENCH_SIZE, chunk: int = DEFAULT_CHUNK) -> float:
    """Read from the drive and return speed in MB/s."""
    k32 = kernel32()
    INVALID = ctypes.c_void_p(-1).value

    handle = k32.CreateFileW(
        drive_path, GENERIC_READ, 1 | 2, None, 3, 0, None,
    )
    if not handle or handle == INVALID:
        return 0.0

    buf = ctypes.create_string_buffer(chunk)
    read_total = 0
    start = time.perf_counter()
    try:
        while read_total < size:
            count = min(chunk, size - read_total)
            n = ctypes.c_ulong()
            ok = k32.ReadFile(handle, buf, count, ctypes.byref(n), None)
            if not ok or n.value == 0:
                break
            read_total += n.value
    finally:
        k32.CloseHandle(handle)

    elapsed = time.perf_counter() - start
    return read_total / elapsed / 1_000_000 if elapsed > 0 else 0.0


def estimate_write_time(
    drive_path: str, image_size: int, *, confirm: bool = False
) -> float:
    """Quick benchmark (2 MB) and estimate total write time in seconds.

    Runs ``benchmark_write`` — i.e. it overwrites the start of the device.
    Destructive: raises ``ValueError`` unless ``confirm=True``.
    """
    bench_size = min(2 * 1024 * 1024, image_size)
    mbps = benchmark_write(
        drive_path,
        size=bench_size,
        chunk=min(bench_size, DEFAULT_CHUNK),
        confirm=confirm,
    )
    if mbps <= 0:
        return 0.0
    return image_size / mbps / 1_000_000
