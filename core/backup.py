"""Backup worker: stream a raw drive into a disk image file.

Read-only with respect to the source drive: the volumes are locked and
dismounted while reading (so the filesystem does not race the reader), but
nothing is written to the drive itself.
"""

import hashlib
import logging
import time
from collections import deque
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from core.deviceio import (
    drive_size,
    flush,
    kernel32,
    lock_volumes,
    open_drive,
    read_bytes,
    unlock_volumes,
)

logger = logging.getLogger("flint")


class BackupWorker(QThread):
    """Copy a raw drive to a file, with progress, rolling speed, a running
    SHA-256 of the bytes read and cancellation support."""

    progress = pyqtSignal(float)
    speed_mbps = pyqtSignal(float)
    written_bytes = pyqtSignal(int)
    total_bytes = pyqtSignal(int)
    eta_seconds = pyqtSignal(int)
    phase = pyqtSignal(str)
    digest = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    CHUNK_SIZE = 4 * 1024 * 1024
    SPEED_WINDOW = 5

    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001
    _ES_DISPLAY_REQUIRED = 0x00000002

    def __init__(
        self,
        drive_path: str,
        out_path: str,
        letters: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.drive_path = drive_path
        self.out_path = out_path
        self.letters = letters or []
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    # Instance-method seams (unit tests bind fakes here; the production
    # implementations delegate to core.deviceio).
    def _open_drive(self) -> Any:
        return open_drive(self.drive_path, write=False)

    def _drive_size(self, handle: Any) -> int:
        return drive_size(handle)

    def _lock_volumes(self) -> list[Any]:
        return lock_volumes(self.letters)

    def _unlock_volumes(self, held: list[Any]) -> None:
        unlock_volumes(held)

    def _read_chunk(self, handle: Any, count: int) -> bytes:
        return read_bytes(handle, count)

    def _flush(self, handle: Any) -> None:
        flush(handle)

    def _write_to_file(self, out_file: Any, data: bytes) -> None:
        out_file.write(data)

    def run(self) -> None:
        kernel32().SetThreadExecutionState(
            self._ES_CONTINUOUS
            | self._ES_SYSTEM_REQUIRED
            | self._ES_DISPLAY_REQUIRED
        )
        try:
            self.phase.emit("Locking drive")
            volumes = self._lock_volumes()
            try:
                self._run_inner()
            finally:
                self._unlock_volumes(volumes)
        except Exception as exc:
            logger.exception("BackupWorker.run failed")
            self.finished.emit(False, str(exc))
        finally:
            kernel32().SetThreadExecutionState(self._ES_CONTINUOUS)

    def _run_inner(self) -> None:
        handle = self._open_drive()
        out_file = None
        try:
            total = self._drive_size(handle)
            if total <= 0:
                raise OSError("unable to determine drive size")
            self.total_bytes.emit(total)
            self.phase.emit("Backing up")
            out_file = open(self.out_path, "wb", buffering=0)  # noqa: SIM115
            digest = hashlib.sha256()
            done = 0
            durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
            sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
            while done < total:
                if self._canceled:
                    break
                chunk_start = time.perf_counter()
                data = self._read_chunk(
                    handle, min(self.CHUNK_SIZE, total - done)
                )
                if not data:
                    raise OSError("read-back ended before the end of the drive")
                self._write_to_file(out_file, data)
                digest.update(data)
                durations.append(time.perf_counter() - chunk_start)
                sizes.append(len(data))
                done += len(data)

                window_bytes = sum(sizes)
                window_time = sum(durations)
                if window_time > 0 and window_bytes > 0:
                    bytes_per_sec = window_bytes / window_time
                    speed = bytes_per_sec / 1_000_000
                    remaining = (total - done) / bytes_per_sec
                else:
                    speed = 0.0
                    remaining = 0.0

                self.progress.emit(done / total * 100.0)
                self.speed_mbps.emit(speed)
                self.written_bytes.emit(done)
                self.eta_seconds.emit(int(remaining))
            if not self._canceled:
                self.phase.emit("Flushing")
                self._flush(handle)
                self.digest.emit(digest.hexdigest())
        except Exception as exc:
            self.finished.emit(False, str(exc))
            return
        finally:
            if out_file is not None:
                try:
                    out_file.close()
                except OSError:
                    pass
            kernel32().CloseHandle(handle)

        if self._canceled:
            self.finished.emit(False, "cancelled")
            return
        self.finished.emit(True, "")
