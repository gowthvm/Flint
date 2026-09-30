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
    _Cancelled,
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
    done = pyqtSignal(bool, str)

    CHUNK_SIZE = 4 * 1024 * 1024
    SPEED_WINDOW = 5

    def __init__(
        self,
        source_path: str,
        target_path: str,
        source_letters: list[str] | None = None,
        target_letters: list[str] | None = None,
        cancel_event: Event | None = None,
        zero_tail: bool = True,
    ) -> None:
        super().__init__()
        self.source_path = source_path
        self.target_path = target_path
        self.source_letters = source_letters or []
        self.target_letters = target_letters or []
        self.cancel_event = cancel_event
        # B08: zero-fill the target tail (source_size → capacity) so a larger
        # target does not keep a remnant of its previous contents.
        self.zero_tail = zero_tail
        self._canceled = False
        self._finished = False

    def cancel(self) -> None:
        self._canceled = True

    def _emit_finished(self, ok: bool, message: str) -> None:
        # L04: exactly one "done" per run — run()'s outer except must not
        # re-emit after _run_inner already reported (mirror UsbWriter).
        if self._finished:
            return
        self._finished = True
        self.done.emit(ok, message)

    def _cancel_requested(self) -> bool:
        return self._canceled or (
            self.cancel_event is not None and self.cancel_event.is_set()
        )

    # Instance-method seams (unit tests bind fakes here).
    def _open_source(self) -> Any:
        # B03: letterless source => nothing to FSCTL-lock, so the handle is
        # the lock (dwShareMode = 0).  With letters the volume locks taken by
        # run() do the locking and the handle stays shareable.
        return open_drive(
            self.source_path, write=False, exclusive=not self.source_letters
        )

    def _open_target(self) -> Any:
        return open_drive(
            self.target_path, write=True, exclusive=not self.target_letters
        )

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
        write_bytes_retry(
            handle,
            data,
            max_retries=3,
            is_cancelled=self._cancel_requested,
        )

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
        except _Cancelled:
            # B06: _Cancelled carries no message and is neither OSError nor
            # ValueError; without this handler the CLI would report a blank
            # failure (exit 1) instead of the "cancelled" sentinel (exit 2).
            logger.info("CloneWorker.run: cancelled")
            self._emit_finished(False, "cancelled")
        except Exception as exc:
            logger.exception("CloneWorker.run failed")
            # L04: guarded — _run_inner may already have emitted.
            self._emit_finished(False, str(exc) or type(exc).__name__)
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
            # B08: the job covers copy (source bytes) plus the zero-fill of
            # the target tail, so one monotonic 0→100 bar spans both phases
            # (L17: no phase may restart the percentage).
            tail = (target_total - total) if self.zero_tail else 0
            job_total = total + tail
            self.total_bytes.emit(job_total)
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
                    remaining = (job_total - done) / bytes_per_sec
                else:
                    speed = 0.0
                    remaining = 0.0

                self.progress.emit(done / job_total * 100.0)
                self.speed_mbps.emit(speed)
                self.written_bytes.emit(done)
                self.eta_seconds.emit(int(remaining))
            if not self._cancel_requested() and tail:
                self._zero_fill(target, total, target_total, job_total)
            if not self._cancel_requested():
                self.phase.emit("Flushing")
                self._flush(target)
                # target_end == total + tail: when zero_tail is off the tail
                # was never rewritten, so verification stops at the source size.
                self._verify_clone(source, target, total, job_total)
        except _Cancelled:
            # B06: cancel fired inside read/write_bytes_retry's loop. Report
            # the "cancelled" sentinel the CLI and GUI map to exit 2 / the
            # cancelled state, never an empty message.
            logger.info("CloneWorker._run_inner: cancelled mid-retry")
            self._emit_finished(False, "cancelled")
            return
        except Exception as exc:
            self._emit_finished(False, str(exc) or type(exc).__name__)
            return
        finally:
            kernel32().CloseHandle(source)
            if target is not None:
                kernel32().CloseHandle(target)

        if self._cancel_requested():
            self._emit_finished(False, "cancelled")
            return
        self._emit_finished(True, "")

    def _zero_fill(
        self, target: Any, start: int, end: int, job_total: int
    ) -> None:
        """B08: erase the target tail [start, end) with zeros, chunked and
        cancel-aware.  Progress continues from the copy phase (already past
        ``start / job_total``) up to 100 % — it never restarts (L17)."""
        self.phase.emit("Zero-filling")
        zeros = b"\x00" * self.CHUNK_SIZE
        offset = start
        durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
        sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
        while offset < end:
            if self._cancel_requested():
                return
            count = min(self.CHUNK_SIZE, end - offset)
            chunk_start = time.perf_counter()
            self._write_chunk(target, zeros[:count])
            durations.append(time.perf_counter() - chunk_start)
            sizes.append(count)
            offset += count

            window_bytes = sum(sizes)
            window_time = sum(durations)
            if window_time > 0 and window_bytes > 0:
                bytes_per_sec = window_bytes / window_time
                speed = bytes_per_sec / 1_000_000
                remaining = (job_total - offset) / bytes_per_sec
            else:
                speed = 0.0
                remaining = 0.0

            self.progress.emit(offset / job_total * 100.0)
            self.speed_mbps.emit(speed)
            self.written_bytes.emit(offset)
            self.eta_seconds.emit(int(remaining))

    def _verify_clone(
        self, source: Any, target: Any, source_end: int, target_end: int
    ) -> None:
        """Read both devices back and fail on the first differing region.

        Covers the *whole* target (B08): ``[0, source_end)`` is compared
        against the source, then ``[source_end, target_end)`` must be zeros
        (the tail this clone just wrote).  ``target_end < source_end`` is
        impossible; callers pass ``target_end == source_end`` when zero-fill
        was disabled.

        L17: no progress is re-emitted here — the copy/zero-fill phases
        already reached 100 % and a watcher must not see the percentage
        restart during verification.
        """
        self._seek(source, 0)
        self._seek(target, 0)
        self.phase.emit("Verifying clone")
        checked = 0
        while checked < source_end:
            if self._cancel_requested():
                return
            count = min(self.CHUNK_SIZE, source_end - checked)
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
            if not source_data:
                raise OSError("clone verification read ended early")
        while checked < target_end:
            if self._cancel_requested():
                return
            count = min(self.CHUNK_SIZE, target_end - checked)
            tail_data = self._read_target_chunk(target, count)
            if len(tail_data) != count:
                raise OSError(
                    f"clone verification failed at byte {checked + len(tail_data):,}"
                )
            stale = next(
                (index for index, byte in enumerate(tail_data) if byte),
                None,
            )
            if stale is not None:
                raise OSError(
                    f"clone verification failed at byte {checked + stale:,}"
                )
            checked += count
