"""Drive speed benchmark — measure sequential read/write throughput."""

import ctypes
import logging
import os
import time
from typing import Any

logger = logging.getLogger("flint")

DEFAULT_BENCH_SIZE = 64 * 1024 * 1024  # 64 MB
DEFAULT_CHUNK = 8 * 1024 * 1024  # 8 MB


def _kernel32() -> Any:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_ulong
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_ulong
    return kernel32


def benchmark_write(drive_path: str, size: int = DEFAULT_BENCH_SIZE, chunk: int = DEFAULT_CHUNK) -> float:
    """Write a test pattern to the drive and return speed in MB/s."""
    kernel32 = _kernel32()
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID = ctypes.c_void_p(-1).value

    handle = kernel32.CreateFileW(
        drive_path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID:
        return 0.0

    pattern = os.urandom(chunk)
    written = 0
    start = time.perf_counter()
    try:
        while written < size:
            buf = ctypes.create_string_buffer(pattern)
            n = ctypes.c_ulong()
            ok = kernel32.WriteFile(handle, buf, len(pattern), ctypes.byref(n), None)
            if not ok or n.value != len(pattern):
                break
            written += n.value
    finally:
        kernel32.CloseHandle(handle)

    elapsed = time.perf_counter() - start
    return written / elapsed / 1_000_000 if elapsed > 0 else 0.0


def benchmark_read(drive_path: str, size: int = DEFAULT_BENCH_SIZE, chunk: int = DEFAULT_CHUNK) -> float:
    """Read from the drive and return speed in MB/s."""
    kernel32 = _kernel32()
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    INVALID = ctypes.c_void_p(-1).value

    handle = kernel32.CreateFileW(
        drive_path, GENERIC_READ, 1 | 2, None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID:
        return 0.0

    buf = ctypes.create_string_buffer(chunk)
    read_total = 0
    start = time.perf_counter()
    try:
        while read_total < size:
            n = ctypes.c_ulong()
            ok = kernel32.ReadFile(handle, buf, chunk, ctypes.byref(n), None)
            if not ok or n.value == 0:
                break
            read_total += n.value
    finally:
        kernel32.CloseHandle(handle)

    elapsed = time.perf_counter() - start
    return read_total / elapsed / 1_000_000 if elapsed > 0 else 0.0


def estimate_write_time(drive_path: str, image_size: int) -> float:
    """Quick benchmark (2 MB) and estimate total write time in seconds."""
    bench_size = min(2 * 1024 * 1024, image_size)
    mbps = benchmark_write(drive_path, size=bench_size, chunk=min(bench_size, DEFAULT_CHUNK))
    if mbps <= 0:
        return 0.0
    return image_size / mbps / 1_000_000
