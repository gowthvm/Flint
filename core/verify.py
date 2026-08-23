import ctypes
import hashlib
import logging
import os
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger("flint")

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
SECTOR_SIZE = 4096
MAX_MISMATCHES = 20
# Mismatch entries only keep a bounded window of the differing chunk
# starting at its first differing byte: full 8-320 MiB chunks held in memory
# caused multi-GB retention on large images. Offsets and lengths are
# preserved; the window is enough context to diagnose corruption.
MISMATCH_SAMPLE_SIZE = SECTOR_SIZE

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _Cancelled(Exception):
    """Raised internally when the caller's cancel callback fires."""


def _kernel32() -> Any:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = ctypes.c_ulong
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_ulong
    kernel32.SetFilePointerEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        ctypes.c_ulong,
    ]
    kernel32.SetFilePointerEx.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_ulong
    return kernel32


def _open_reader(path: str) -> Any:
    """Open a file or raw device for reading; return the handle or None."""
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        path,
        0x80000000,  # GENERIC_READ
        0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    return handle


def _device_size(handle: Any) -> int:
    kernel32 = _kernel32()
    length = ctypes.c_ulonglong()
    returned = ctypes.c_ulong()
    ok = kernel32.DeviceIoControl(
        handle,
        0x0007405C,  # IOCTL_DISK_GET_LENGTH_INFO
        None,
        0,
        ctypes.byref(length),
        ctypes.sizeof(length),
        ctypes.byref(returned),
        None,
    )
    if not ok or length.value <= 0:
        return 0
    return length.value


def _seek(handle: Any, offset: int, retries: int) -> bool:
    """Position ``handle`` at ``offset``, retrying failed calls.

    Returns False when every attempt failed (``_Cancelled`` is not raised;
    callers keep the cancel check in the read loop).
    """
    kernel32 = _kernel32()
    for _ in range(retries + 1):
        position = ctypes.c_longlong()
        if kernel32.SetFilePointerEx(
            handle, ctypes.c_longlong(offset), ctypes.byref(position), 0
        ):
            return True
        time.sleep(0.05)
    return False


def _read_chunk(
    handle: Any,
    buffer: Any,
    count: int,
    retries: int,
    is_cancelled: Callable[[], bool] | None,
) -> int | None:
    """Read up to ``count`` bytes, retrying failed reads ``retries`` times.

    Returns the number of bytes read (0 = end of device) or ``None`` when
    every attempt failed. Raises ``_Cancelled`` when the cancel callback
    fires between attempts.
    """
    kernel32 = ctypes.windll.kernel32
    read = ctypes.c_ulong()
    for attempt in range(retries + 1):
        if is_cancelled is not None and is_cancelled():
            raise _Cancelled()
        if kernel32.ReadFile(handle, buffer, count, ctypes.byref(read), None):
            return read.value
        if attempt < retries:
            time.sleep(0.05)
    return None


def compute_sha256(
    path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Streaming SHA-256 of a file or raw device path.

    Returns ``(True, hexdigest)`` on success or ``(False, message)`` on
    failure. ``progress`` is called with ``(bytes_done, bytes_total)``.
    """
    handle = _open_reader(path)
    if handle is None:
        return False, f"could not open {path} for reading"
    try:
        if path.startswith("\\\\.\\"):
            size = _device_size(handle)
        else:
            size = os.path.getsize(path)
        if size <= 0:
            return False, "nothing to hash"
        digest = hashlib.sha256()
        done = 0
        while done < size:
            count = min(chunk_size, size - done)
            buffer = ctypes.create_string_buffer(count)
            try:
                nread = _read_chunk(handle, buffer, count, 0, is_cancelled)
            except _Cancelled:
                return False, "cancelled"
            if nread is None or nread == 0:
                return False, "read failed before the end of the device"
            digest.update(buffer.raw[:nread])
            done += nread
            if progress is not None:
                progress(done, size)
        return True, digest.hexdigest()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def verify_device(
    device_path: str,
    source_iso: str | None = None,
    expected_sha256: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    retries: int = 3,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    scan_full_drive: bool = False,
    sample_mode: bool = False,
) -> dict[str, Any]:
    """Read a device back and verify it against a source image.

    Returns a structured result dict:

    - ``ok``          - True when nothing failed (no mismatches, no
                        unreadable sectors, digest matches when expected)
    - ``mismatches``  - ``[(offset, length, sample_offset, expected,
                        actual)]`` byte comparisons that differ (only with
                        ``source_iso``, capped at ``MAX_MISMATCHES``
                        entries). ``sample_offset`` is where the stored
                        window starts inside the chunk; expected/actual hold
                        at most ``MISMATCH_SAMPLE_SIZE`` bytes from there
    - ``bad_sectors`` - 4096-aligned offsets that could not be read after
                        ``retries`` retries; those chunks are skipped
    - ``digest``      - SHA-256 of the bytes that were read back
    - ``speed_mbps``  - average read-back throughput
    - ``error``       - error/cancel message ("" when everything ran)

    With ``source_iso`` the read-back is byte-compared against the image
    (mismatch offsets are reported). With only ``expected_sha256`` the
    digest is compared against it. With neither, the call is a pure
    bad-block scan.
    """
    result: dict[str, Any] = {
        "ok": False,
        "mismatches": [],
        "bad_sectors": [],
        "digest": "",
        "speed_mbps": 0.0,
        "drive_size": 0,
        "error": "",
    }
    handle = _open_reader(device_path)
    if handle is None:
        result["error"] = f"could not open device for read-back: {device_path}"
        return result
    iso_file = None
    try:
        if device_path.startswith("\\\\.\\"):
            size = _device_size(handle)
            if size <= 0:
                result["error"] = (
                    "could not determine device size for verification"
                )
                return result
        else:
            size = os.path.getsize(device_path)
        result["drive_size"] = size
        iso_size = None
        if source_iso is not None:
            iso_size = os.path.getsize(source_iso)
            if iso_size > size:
                result["error"] = (
                    "drive is smaller than the image; verification is "
                    "not meaningful"
                )
                return result
        if scan_full_drive and source_iso is not None:
            verify_size = size
        else:
            verify_size = iso_size if iso_size is not None else size
        if verify_size <= 0:
            result["error"] = "nothing to verify"
            return result
        if sample_mode and verify_size > 16 * 1024 * 1024:
            # Quick sample: first 8MB, last 8MB, 4 random 8MB chunks
            import random
            SAMPLE_CHUNK = 8 * 1024 * 1024
            regions = []
            # First chunk
            regions.append((0, min(SAMPLE_CHUNK, verify_size)))
            # Last chunk
            last_start = max(verify_size - SAMPLE_CHUNK, SAMPLE_CHUNK)
            regions.append((last_start, min(SAMPLE_CHUNK, verify_size - last_start)))
            # 4 random middle chunks
            for _ in range(4):
                rand_start = random.randint(SAMPLE_CHUNK, max(SAMPLE_CHUNK, verify_size - SAMPLE_CHUNK * 2))
                regions.append((rand_start, min(SAMPLE_CHUNK, verify_size - rand_start)))
            # Sort and merge overlapping regions
            regions.sort()
            # Read and verify only these regions
            digest = hashlib.sha256()
            done = 0
            start = time.perf_counter()
            for region_start, region_len in regions:
                if is_cancelled is not None and is_cancelled():
                    result["error"] = "cancelled"
                    return result
                if not _seek(handle, region_start, retries):
                    result["error"] = "could not seek for sample verification"
                    return result
                region_done = 0
                while region_done < region_len:
                    count = min(chunk_size, region_len - region_done)
                    buffer = ctypes.create_string_buffer(count)
                    try:
                        nread = _read_chunk(handle, buffer, count, retries, is_cancelled)
                    except _Cancelled:
                        result["error"] = "cancelled"
                        return result
                    if nread is None:
                        result["bad_sectors"].append(region_start + region_done - (region_start + region_done) % SECTOR_SIZE)
                        region_done += count
                        if iso_file is not None:
                            iso_file.seek(region_start + region_done)
                        if not _seek(handle, region_start + region_done, retries):
                            result["error"] = "could not reposition for sample verification"
                            return result
                        if progress is not None:
                            progress(done + region_done, verify_size)
                        continue
                    if nread == 0:
                        break
                    data = buffer.raw[:nread]
                    digest.update(data)
                    if iso_file is not None and region_start + region_done < (iso_size or 0):
                        iso_file.seek(region_start + region_done)
                        expected = iso_file.read(nread)
                        if expected != data and len(result["mismatches"]) < MAX_MISMATCHES:
                            first_diff = next((i for i, (a, b) in enumerate(zip(expected, data)) if a != b), 0)
                            win = min(len(data) - first_diff, MISMATCH_SAMPLE_SIZE)
                            result["mismatches"].append((
                                region_start + region_done, nread, first_diff,
                                expected[first_diff:first_diff+win], data[first_diff:first_diff+win],
                            ))
                    region_done += nread
                    done += nread
                    if progress is not None:
                        progress(done, verify_size)
            elapsed = time.perf_counter() - start
            result["speed_mbps"] = done / elapsed / 1_000_000 if elapsed > 0 else 0.0
            result["digest"] = digest.hexdigest()
            result["ok"] = not result["bad_sectors"] and not result["mismatches"] and (
                expected_sha256 is None or result["digest"] == expected_sha256
            )
            return result
        with (
            open(source_iso, "rb")
            if source_iso is not None
            else nullcontext()
        ) as iso_file:
            digest = hashlib.sha256()
            done = 0
            start = time.perf_counter()
            while done < verify_size:
                count = min(chunk_size, verify_size - done)
                buffer = ctypes.create_string_buffer(count)
                try:
                    nread = _read_chunk(
                        handle, buffer, count, retries, is_cancelled
                    )
                except _Cancelled:
                    result["error"] = "cancelled"
                    return result
                if nread is None:
                    result["bad_sectors"].append(done - done % SECTOR_SIZE)
                    done += count
                    # Both the device and the source file stayed at the old
                    # position; skip the chunk on both sides so the tail of
                    # the image is still read, hashed and compared.
                    if iso_file is not None:
                        iso_file.seek(done)
                    if not _seek(handle, done, retries):
                        result["error"] = (
                            "could not reposition the device for read-back"
                        )
                        return result
                    if progress is not None:
                        progress(done, verify_size)
                    continue
                if nread == 0:
                    result["error"] = "read-back ended before the image"
                    return result
                data = buffer.raw[:nread]
                digest.update(data)
                if iso_file is not None and (
                    not scan_full_drive
                    or (iso_size is not None and done < iso_size)
                ):
                    if (
                        scan_full_drive
                        and iso_size is not None
                        and done < iso_size
                        and done + nread > iso_size
                    ):
                        compare_len = iso_size - done
                        expected = iso_file.read(compare_len)
                        cmp_data = data[:compare_len]
                    else:
                        expected = iso_file.read(nread)
                        cmp_data = data
                    if (
                        expected != cmp_data
                        and len(result["mismatches"]) < MAX_MISMATCHES
                    ):
                        # Snapshot a bounded window starting at the FIRST
                        # differing byte (a full chunk can be hundreds of
                        # MB; corruption can sit anywhere inside it). The
                        # sample offset within the chunk is stored too, so
                        # callers can reconstruct absolute positions.
                        first_diff = next(
                            (
                                i
                                for i, (a, b) in enumerate(
                                    zip(expected, cmp_data)
                                )
                                if a != b
                            ),
                            0,
                        )
                        win = min(
                            len(cmp_data) - first_diff, MISMATCH_SAMPLE_SIZE
                        )
                        result["mismatches"].append(
                            (
                                done,
                                nread,
                                first_diff,
                                expected[first_diff : first_diff + win],
                                cmp_data[first_diff : first_diff + win],
                            )
                        )
                done += nread
                if progress is not None:
                    progress(done, verify_size)
            elapsed = time.perf_counter() - start
            result["speed_mbps"] = (
                done / elapsed / 1_000_000 if elapsed > 0 else 0.0
            )
            result["digest"] = digest.hexdigest()
            result["ok"] = (
                not result["bad_sectors"]
                and not result["mismatches"]
                and (
                    expected_sha256 is None
                    or result["digest"] == expected_sha256
                )
            )
            return result
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def scan_bad_sectors(
    device_path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    retries: int = 3,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Read a device looking for unreadable sectors (failed reads retried).

    Returns the same structured result as ``verify_device`` with neither
    ``source_iso`` nor ``expected_sha256``: ``ok`` / ``bad_sectors`` /
    ``digest`` / ``speed_mbps`` / ``error``.
    """
    return verify_device(
        device_path,
        chunk_size=chunk_size,
        retries=retries,
        progress=progress,
        is_cancelled=is_cancelled,
    )


def whole_drive_scan(
    device_path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    retries: int = 3,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    raw = verify_device(
        device_path,
        chunk_size=chunk_size,
        retries=retries,
        progress=progress,
        is_cancelled=is_cancelled,
    )
    bad = [{"offset": off, "length": chunk_size} for off in raw["bad_sectors"]]
    return {
        "ok": raw["ok"],
        "bad_sectors": bad,
        "digest": raw["digest"],
        "speed_mbps": raw["speed_mbps"],
        "drive_size": raw["drive_size"],
        "error": raw["error"],
    }


def drive_health_summary(result: dict[str, Any]) -> str:
    """Return a human-readable health percentage string."""
    drive_size = result.get("drive_size", 0)
    bad_sectors = result.get("bad_sectors", [])
    if drive_size <= 0:
        return "unknown"
    bad_bytes = sum(bs.get("length", 0) for bs in bad_sectors) if isinstance(bad_sectors, list) else len(bad_sectors) * 4096
    healthy_pct = (1.0 - bad_bytes / drive_size) * 100
    if bad_bytes == 0:
        return "100% healthy"
    return f"{healthy_pct:.4f}% healthy ({len(bad_sectors)} bad region(s))"


def hash_drive(
    drive_path: str,
    size: int | None,
    expected_sha256: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Read `size` bytes back from a raw drive (or the whole drive when
    `size` is None) and compare against the expected SHA-256 digest.

    Returns (ok, hexdigest) on success or (False, message) on failure."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = ctypes.c_ulong
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_ulong
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    CHUNK = 4 * 1024 * 1024

    handle = kernel32.CreateFileW(
        drive_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False, f"could not open drive for read-back: {drive_path}"
    try:
        if size is None:
            length = ctypes.c_ulonglong()
            returned = ctypes.c_ulong()
            ok = kernel32.DeviceIoControl(
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
                return False, "could not determine drive size for verification"
            size = length.value
        digest = hashlib.sha256()
        remaining = size
        done = 0
        while remaining > 0:
            if is_cancelled is not None and is_cancelled():
                return False, "cancelled"
            count = min(CHUNK, remaining)
            buffer = ctypes.create_string_buffer(count)
            read = ctypes.c_ulong()
            ok = kernel32.ReadFile(
                handle,
                buffer,
                count,
                ctypes.byref(read),
                None,
            )
            if not ok or read.value == 0:
                return False, "drive read-back ended before the image"
            digest.update(buffer.raw[: read.value])
            remaining -= read.value
            done += read.value
            if progress is not None:
                progress(done, size)
        result = digest.hexdigest()
        if is_cancelled is not None and is_cancelled():
            return False, "cancelled"
        if expected_sha256 is not None and result != expected_sha256:
            return False, "verification failed: hash mismatch"
        return True, result
    finally:
        kernel32.CloseHandle(handle)


class VerifyWorker(QThread):
    progress = pyqtSignal(float)
    stats = pyqtSignal(int, int)
    finished = pyqtSignal(bool, str)

    def __init__(
        self,
        drive_path: str,
        expected_sha256: str,
        size: int | None,
    ) -> None:
        super().__init__()
        self._drive_path = drive_path
        self._expected_sha256 = expected_sha256
        self._size = size
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        def on_progress(done: int, total: int) -> None:
            self.progress.emit(done / total * 100.0)
            self.stats.emit(done, total)

        try:
            ok, result = hash_drive(
                self._drive_path,
                self._size,
                self._expected_sha256,
                progress=on_progress,
                is_cancelled=lambda: self._cancelled,
            )
        except Exception as exc:
            # Never let a worker thread die silently: the UI would stay
            # blocked with no way out.
            logger.exception("VerifyWorker.run failed")
            self.finished.emit(False, str(exc) or "verification failed")
            return
        if not ok:
            self.finished.emit(False, result)
            return
        self.progress.emit(100.0)
        self.finished.emit(True, result)
