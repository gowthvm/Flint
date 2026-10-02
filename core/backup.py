"""Backup worker: stream a raw drive into a disk image file.

Read-only with respect to the source drive: the volumes are locked and
dismounted while reading (so the filesystem does not race the reader), but
nothing is written to the drive itself.
"""

import hashlib
import logging
import os
import shutil
import time
from collections import deque
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from core.deviceio import (
    ES_CONTINUOUS,
    ES_DISPLAY_REQUIRED,
    ES_SYSTEM_REQUIRED,
    drive_size,
    kernel32,
    lock_volumes,
    open_drive,
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
    done = pyqtSignal(bool, str)

    CHUNK_SIZE = 4 * 1024 * 1024
    SPEED_WINDOW = 5

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
        self._finished = False

    def cancel(self) -> None:
        self._canceled = True

    def _emit_finished(self, ok: bool, message: str) -> None:
        # L04: exactly one "done" per run.  run()'s outer except used to
        # re-emit after _run_inner had already reported success/failure
        # (e.g. when _unlock_volumes raised) — mirror UsbWriter's guard.
        if self._finished:
            return
        self._finished = True
        self.done.emit(ok, message)

    # Instance-method seams (unit tests bind fakes here; the production
    # implementations delegate to core.deviceio).
    def _open_drive(self) -> Any:
        # B03: with no mounted letters there is nothing to FSCTL-lock, so the
        # read handle itself must act as the lock (dwShareMode = 0).  When
        # letters exist the volume locks taken by run() do the locking and
        # the handle stays shareable, matching UsbWriter's pattern.
        return open_drive(self.drive_path, write=False, exclusive=not self.letters)

    def _drive_size(self, handle: Any) -> int:
        return drive_size(handle)

    def _lock_volumes(self) -> list[Any]:
        return lock_volumes(self.letters)

    def _unlock_volumes(self, held: list[Any]) -> None:
        unlock_volumes(held)

    def _read_chunk(self, handle: Any, count: int) -> bytes:
        from core.deviceio import read_bytes_retry

        result = read_bytes_retry(handle, count, retries=3)
        if result is None:
            raise OSError("read failed after retries")
        return result

    def _free_space(self, directory: str) -> int:
        return shutil.disk_usage(directory).free

    def _check_free_space(self, total: int) -> None:
        # B07: the source is a raw device, so "needed" is the device capacity.
        # Fail here, before the destination is opened for writing at all.
        directory = os.path.dirname(os.path.abspath(self.out_path))
        free = self._free_space(directory)
        if free < total:
            raise OSError(
                f"not enough free space in {directory}: "
                f"{free:,} bytes available, {total:,} bytes needed "
                "for the backup"
            )

    def _write_to_file(self, out_file: Any, data: bytes) -> None:
        out_file.write(data)

    def run(self) -> None:
        kernel32().SetThreadExecutionState(
            ES_CONTINUOUS
            | ES_SYSTEM_REQUIRED
            | ES_DISPLAY_REQUIRED
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
            # L04: guarded — _run_inner may already have emitted.
            self._emit_finished(False, str(exc))
        finally:
            kernel32().SetThreadExecutionState(ES_CONTINUOUS)

    def _run_inner(self) -> None:
        handle = self._open_drive()
        out_file = None
        success = False
        # B07: never touch the destination until the image is complete and
        # verified — stream into a sibling .partial file and os.replace it
        # onto out_path only on success.
        partial_path = self.out_path + ".partial"
        try:
            total = self._drive_size(handle)
            if total <= 0:
                raise OSError("unable to determine drive size")
            self.total_bytes.emit(total)
            self._check_free_space(total)
            self.phase.emit("Backing up")
            out_file = open(partial_path, "wb", buffering=0)  # noqa: SIM115
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
                # The drive handle is read-only (GENERIC_READ only), so
                # FlushFileBuffers would fail with ERROR_ACCESS_DENIED and
                # abort the backup; the source is never written to, so there
                # is nothing to flush on that side.  Only the output file
                # needs to reach the disk.
                if out_file is not None:
                    try:
                        os.fsync(out_file.fileno())
                    except OSError:
                        pass
                # Verify the backup file matches what we read from the drive
                self.phase.emit("Verifying backup")
                verify_digest = hashlib.sha256()
                with open(partial_path, "rb") as vf:
                    while vchunk := vf.read(self.CHUNK_SIZE):
                        verify_digest.update(vchunk)
                if verify_digest.hexdigest() != digest.hexdigest():
                    raise OSError("backup verification failed: file does not match drive contents")
                if out_file is not None:
                    out_file.close()
                    out_file = None
                # Success only: publish the verified image atomically.
                os.replace(partial_path, self.out_path)
                success = True
                self.digest.emit(digest.hexdigest())
        except Exception as exc:
            self._emit_finished(False, str(exc))
            return
        finally:
            if out_file is not None:
                try:
                    out_file.close()
                except OSError:
                    pass
            # Remove the partial image on error or cancellation; any
            # pre-existing file at out_path is left untouched (B07).
            if (self._canceled or not success) and os.path.isfile(partial_path):
                try:
                    os.unlink(partial_path)
                except OSError:
                    pass
            kernel32().CloseHandle(handle)

        if self._canceled:
            self._emit_finished(False, "cancelled")
            return
        self._emit_finished(True, "")
