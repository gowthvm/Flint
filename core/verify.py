import ctypes
import hashlib
import logging
import os
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from core.deviceio import (
    TRANSIENT_SEEK_ERRORS,
    drive_size,
    kernel32,
    open_drive,
    read_bytes_retry,
    seek_retry,
)

logger = logging.getLogger("flint")

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
SECTOR_SIZE = 4096
MAX_MISMATCHES = 20
# Mismatch entries only keep a bounded window of the differing chunk
# starting at its first differing byte: full 8-320 MiB chunks held in memory
# caused multi-GB retention on large images. Offsets and lengths are
# preserved; the window is enough context to diagnose corruption.
MISMATCH_SAMPLE_SIZE = SECTOR_SIZE


class _Cancelled(Exception):
    """Raised internally when the caller's cancel callback fires."""


def _device_size(handle: Any) -> int:
    try:
        return drive_size(handle)
    except OSError:
        return 0


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
    try:
        handle = open_drive(path, write=False)
    except OSError:
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
            try:
                data = read_bytes_retry(handle, count, retries=0, is_cancelled=is_cancelled)
            except Exception:
                return False, "cancelled"
            if data is None or len(data) == 0:
                return False, "read failed before the end of the device"
            digest.update(data)
            done += len(data)
            if progress is not None:
                progress(done, size)
        return True, digest.hexdigest()
    finally:
        kernel32().CloseHandle(handle)


def verify_device(
    device_path: str,
    source_iso: str | None = None,
    expected_sha256: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    retries: int = 3,
    progress: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    scan_full_drive: bool = False,
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
    try:
        handle = open_drive(device_path, write=False)
    except OSError:
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
        with (
            open(source_iso, "rb")
            if source_iso is not None
            else nullcontext()
        ) as iso_file:
            digest = hashlib.sha256()
            done = 0
            t_start = time.perf_counter()
            while done < verify_size:
                count = min(chunk_size, verify_size - done)
                try:
                    data = read_bytes_retry(
                        handle, count, retries=retries, is_cancelled=is_cancelled
                    )
                except Exception:
                    result["error"] = "cancelled"
                    return result
                if data is None:
                    result["bad_sectors"].append(done - done % SECTOR_SIZE)
                    done += count
                    # Both the device and the source file stayed at the old
                    # position; skip the chunk on both sides so the tail of
                    # the image is still read, hashed and compared.
                    if iso_file is not None:
                        iso_file.seek(done)
                    if not seek_retry(handle, done, retries):
                        result["error"] = (
                            "could not reposition the device for read-back"
                        )
                        return result
                    if progress is not None:
                        progress(done, verify_size)
                    continue
                if len(data) == 0:
                    result["error"] = "read-back ended before the image"
                    return result
                nread = len(data)
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
            elapsed = time.perf_counter() - t_start
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
        kernel32().CloseHandle(handle)


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
    bad_bytes = sum(
        (bs.get("length", 0) if isinstance(bs, dict) else 4096)
        for bs in bad_sectors
    ) if isinstance(bad_sectors, list) else 0
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
    CHUNK = 4 * 1024 * 1024

    try:
        handle = open_drive(drive_path, write=False)
    except OSError:
        return False, f"could not open drive for read-back: {drive_path}"
    try:
        if size is None:
            try:
                size = drive_size(handle)
            except OSError:
                return False, "could not determine drive size for verification"
        digest = hashlib.sha256()
        remaining = size
        done = 0
        while remaining > 0:
            if is_cancelled is not None and is_cancelled():
                return False, "cancelled"
            count = min(CHUNK, remaining)
            data = read_bytes_retry(handle, count, retries=3, is_cancelled=is_cancelled)
            if data is None or len(data) == 0:
                return False, f"drive read-back ended at byte {done:,}"
            digest.update(data)
            remaining -= len(data)
            done += len(data)
            if progress is not None:
                progress(done, size)
        result = digest.hexdigest()
        if is_cancelled is not None and is_cancelled():
            return False, "cancelled"
        if expected_sha256 is not None and result != expected_sha256:
            return False, "verification failed: hash mismatch"
        return True, result
    finally:
        kernel32().CloseHandle(handle)


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
