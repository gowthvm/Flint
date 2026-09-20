"""Clone worker: copy one raw drive to another, sector by sector.

Both drives' volumes are locked and dismounted for the duration of the
copy. The target is completely overwritten; the source is read-only.
"""

import ctypes
import logging
import time
from collections import deque
from threading import Event
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from core.deviceio import (
    ES_CONTINUOUS,
    ES_DISPLAY_REQUIRED,
    ES_SYSTEM_REQUIRED,
    drive_size,
    flush,
    kernel32,
    lock_volumes,
    open_drive,
    read_bytes_retry,
    unlock_volumes,
    write_bytes_retry,
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

    def __init__(
        self,
        source_path: str,
        target_path: str,
        source_letters: list[str] | None = None,
        target_letters: list[str] | None = None,
        cancel_event: Event | None = None,
    ) -> None:
        super().__init__()
        self.source_path = source_path
        self.target_path = target_path
        self.source_letters = source_letters or []
        self.target_letters = target_letters or []
        self.cancel_event = cancel_event
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    def _cancel_requested(self) -> bool:
        return self._canceled or (
            self.cancel_event is not None and self.cancel_event.is_set()
        )

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
        result = read_bytes_retry(handle, count, retries=3, is_cancelled=self._cancel_requested)
        if result is None:
            raise OSError("read failed after retries")
        return result

    def _read_target_chunk(self, handle: Any, count: int) -> bytes:
        result = read_bytes_retry(handle, count, retries=3, is_cancelled=self._cancel_requested)
        if result is None:
            raise OSError("read failed after retries")
        return result

    def _seek(self, handle: Any, offset: int) -> None:
        position = ctypes.c_longlong()
        if not kernel32().SetFilePointerEx(
            handle, ctypes.c_longlong(offset), ctypes.byref(position), 0
        ):
            raise OSError(f"could not seek clone handle to byte {offset:,}")

    def _write_chunk(self, handle: Any, data: bytes) -> None:
        write_bytes_retry(handle, data, max_retries=3)

    def _flush(self, handle: Any) -> None:
        flush(handle)

    def run(self) -> None:
        kernel32().SetThreadExecutionState(
            ES_CONTINUOUS
            | ES_SYSTEM_REQUIRED
            | ES_DISPLAY_REQUIRED
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
            kernel32().SetThreadExecutionState(ES_CONTINUOUS)

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
                if self._cancel_requested():
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
            if not self._cancel_requested():
                self.phase.emit("Flushing")
                self._flush(target)
                self._verify_clone(source, target, total)
        except Exception as exc:
            self.finished.emit(False, str(exc))
            return
        finally:
            kernel32().CloseHandle(source)
            if target is not None:
                kernel32().CloseHandle(target)

        if self._cancel_requested():
            self.finished.emit(False, "cancelled")
            return
        self.finished.emit(True, "")

    def _verify_clone(self, source: Any, target: Any, total: int) -> None:
        """Read both devices back and fail on the first differing region."""
        self._seek(source, 0)
        self._seek(target, 0)
        self.phase.emit("Verifying clone")
        checked = 0
        while checked < total:
            if self._cancel_requested():
                return
            count = min(self.CHUNK_SIZE, total - checked)
            source_data = self._read_chunk(source, count)
            target_data = self._read_target_chunk(target, count)
            if source_data != target_data:
                limit = min(len(source_data), len(target_data))
                mismatch = next(
                    (
                        index
                        for index in range(limit)
                        if source_data[index] != target_data[index]
                    ),
                    limit,
                )
                raise OSError(
                    f"clone verification failed at byte {checked + mismatch:,}"
                )
            if len(source_data) != len(target_data):
                raise OSError(
                    f"clone verification failed at byte {checked + limit:,}"
                )
            checked += len(source_data)
            self.progress.emit(checked / total * 100.0)
            self.written_bytes.emit(checked)
            if not source_data:
                raise OSError("clone verification read ended early")
