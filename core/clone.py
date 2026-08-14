"""Clone worker: copy one raw drive to another, sector by sector.

Both drives' volumes are locked and dismounted for the duration of the
copy. The target is completely overwritten; the source is read-only.
"""

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
    write_bytes,
)

logger = logging.getLogger("flint")


class CloneWorker(QThread):
    """Copy ``source_path`` onto ``target_path`` with progress, rolling
    speed and cancellation support."""

    progress = pyqtSignal(float)
    speed_mbps = pyqtSignal(float)
    written_bytes = pyqtSignal(int)
    total_bytes = pyqtSignal(int)
    eta_seconds = pyqtSignal(int)
    phase = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    CHUNK_SIZE = 4 * 1024 * 1024
    SPEED_WINDOW = 5

    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001
    _ES_DISPLAY_REQUIRED = 0x00000002

    def __init__(
        self,
        source_path: str,
        target_path: str,
        source_letters: list[str] | None = None,
        target_letters: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.source_path = source_path
        self.target_path = target_path
        self.source_letters = source_letters or []
        self.target_letters = target_letters or []
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    # Instance-method seams (unit tests bind fakes here).
    def _open_source(self) -> Any:
        return open_drive(self.source_path, write=False)

    def _open_target(self) -> Any:
        return open_drive(self.target_path, write=True)

    def _source_size(self, handle: Any) -> int:
        return drive_size(handle)

    def _target_size(self, handle: Any) -> int:
        return drive_size(handle)

    def _lock_volumes(self) -> list[Any]:
        held = lock_volumes(self.source_letters)
        try:
            held += lock_volumes(self.target_letters)
        except OSError:
            unlock_volumes(held)
            raise
        return held

    def _unlock_volumes(self, held: list[Any]) -> None:
        unlock_volumes(held)

    def _read_chunk(self, handle: Any, count: int) -> bytes:
        return read_bytes(handle, count)

    def _write_chunk(self, handle: Any, data: bytes) -> None:
        write_bytes(handle, data)

    def _flush(self, handle: Any) -> None:
        flush(handle)

    def run(self) -> None:
        kernel32().SetThreadExecutionState(
            self._ES_CONTINUOUS
            | self._ES_SYSTEM_REQUIRED
            | self._ES_DISPLAY_REQUIRED
        )
        try:
            self.phase.emit("Locking drives")
            volumes = self._lock_volumes()
            try:
                self._run_inner()
            finally:
                self._unlock_volumes(volumes)
        except Exception as exc:
            logger.exception("CloneWorker.run failed")
            self.finished.emit(False, str(exc))
        finally:
            kernel32().SetThreadExecutionState(self._ES_CONTINUOUS)

    def _run_inner(self) -> None:
        source = self._open_source()
        target = None
        try:
            target = self._open_target()
        except OSError:
            kernel32().CloseHandle(source)
            raise
        try:
            total = self._source_size(source)
            if total <= 0:
                raise OSError("unable to determine source drive size")
            target_total = self._target_size(target)
            if target_total < total:
                raise OSError("target drive is smaller than the source")
            self.total_bytes.emit(total)
            self.phase.emit("Cloning")
            done = 0
            durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
            sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
            while done < total:
                if self._canceled:
                    break
                chunk_start = time.perf_counter()
                data = self._read_chunk(
                    source, min(self.CHUNK_SIZE, total - done)
                )
                if not data:
                    raise OSError("source read ended before the end of the drive")
                self._write_chunk(target, data)
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
                self._flush(target)
        except Exception as exc:
            self.finished.emit(False, str(exc))
            return
        finally:
            kernel32().CloseHandle(source)
            if target is not None:
                kernel32().CloseHandle(target)

        if self._canceled:
            self.finished.emit(False, "cancelled")
            return
        self.finished.emit(True, "")
