import ctypes
import importlib
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from threading import Event
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from core import diskpart, jobs, persistence
from core import iso as iso_mod
from core import verify as verify_mod
from core.deviceio import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    FILE_FLAG_NO_BUFFERING,
    FILE_FLAG_WRITE_THROUGH,
    drive_size,
    flush,
    kernel32,
    lock_volumes,
    open_drive,
    seek,
    unlock_volumes,
    write_bytes_retry,
)

logger = logging.getLogger("flint")

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024


class _NativeCancel(Exception):
    """Raised from the native progress callback to abort the write."""


def _load_native_writer() -> Any:
    """Return the compiled ``core._native_writer`` module, or ``None``.

    The extension is optional; every caller falls back to the pure-Python
    write path when it is not importable.
    """
    try:
        return importlib.import_module("core._native_writer")
    except ImportError:
        logger.info("native writer not built; using the Python write path")
        return None


def write_stream(
    src: str,
    device: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    use_native: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Copy ``src`` onto ``device`` in ``chunk_size``-sized buffered chunks.

    With ``use_native=True`` the optional compiled extension
    ``core._native_writer`` (CreateFile/WriteFile with FILE_FLAG_NO_BUFFERING
    and aligned buffers) is used when importable; otherwise the write falls
    back to plain buffered Python file IO. ``progress``, when given, is
    called with ``(bytes_done, bytes_total)`` after every chunk. Returns the
    number of bytes written.
    """
    if use_native:
        native_mod = _load_native_writer()
        if native_mod is not None:
            return int(native_mod.native_write(src, device, chunk_size, progress))
    return _python_write_stream(src, device, chunk_size, progress)


def _python_write_stream(
    src: str,
    device: str,
    chunk_size: int,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as source, open(device, "wb") as target:
        while chunk := source.read(chunk_size):
            target.write(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return done


def is_iso_hybrid(iso_path: str) -> bool:
    """True when the image is a hybrid ISO (ISO9660 + bootable MBR).

    Hybrid images must be written raw (DD): a file-by-file copy would lose
    the boot record. Detection is a fast in-process heuristic, see
    ``core.iso.is_hybrid_iso``.
    """
    return iso_mod.is_hybrid_iso(iso_path)


class UsbWriter(QThread):
    progress = pyqtSignal(float)
    speed_mbps = pyqtSignal(float)
    written_bytes = pyqtSignal(int)
    total_bytes = pyqtSignal(int)
    eta_seconds = pyqtSignal(int)
    phase = pyqtSignal(str)
    mode = pyqtSignal(str)
    note = pyqtSignal(str)
    verify_result = pyqtSignal(bool, str, dict)
    finished = pyqtSignal(bool, str)

    SPEED_WINDOW = 5
    _SECTOR_SIZE = 4096

    def __init__(
        self,
        iso_path: str,
        drive_path: str,
        letters: list[str] | None = None,
        partition_scheme: str = "auto",
        target_system: str = "auto",
        filesystem: str = "fat32",
        write_mode: str = "auto",
        persistence: bool = False,
        persistence_size_mb: int = 1024,
        windows_to_go: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        use_native: bool = False,
        verify_after_write: bool = False,
        verify_sha256: bool = True,
        bad_block_scan: bool = False,
        bad_block_retries: int = 3,
        resume: bool = False,
        bypass_tpm: bool = False,
        target_fingerprint: str | None = None,
        manifest_path: str | None = None,
        cancel_event: Event | None = None,
    ) -> None:
        super().__init__()
        self.iso_path = iso_path
        self.drive_path = drive_path
        self.letters = letters or []
        self.partition_scheme = partition_scheme
        self.target_system = target_system
        self.filesystem = filesystem
        self.write_mode = write_mode
        self.persistence = persistence
        self.persistence_size_mb = persistence_size_mb
        self.windows_to_go = windows_to_go
        self.chunk_size = (
            chunk_size if chunk_size >= self._SECTOR_SIZE else DEFAULT_CHUNK_SIZE
        )
        self.chunk_size -= self.chunk_size % self._SECTOR_SIZE
        self.use_native = use_native
        self.verify_after_write = verify_after_write
        self.verify_sha256 = verify_sha256
        self.bad_block_scan = bad_block_scan
        self.bad_block_retries = max(0, int(bad_block_retries))
        self.resume = resume
        self.bypass_tpm = bypass_tpm
        self.target_fingerprint = target_fingerprint or drive_path
        self.manifest_path = manifest_path or (self.iso_path + ".flint_job.json")
        self.cancel_event = cancel_event
        self._canceled = False
        self._finished = False

    def cancel(self) -> None:
        self._canceled = True

    def _cancel_requested(self) -> bool:
        return self._canceled or (
            self.cancel_event is not None and self.cancel_event.is_set()
        )

    def _open_drive(self) -> int:
        return int(open_drive(self.drive_path, write=True, flags=FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH))

    def _drive_size(self, handle: int) -> int:
        return drive_size(handle)

    def _seek_drive(self, handle: int, offset: int) -> None:
        seek(handle, offset)

    def _write_chunk(self, handle: int, data: bytes) -> None:
        write_bytes_retry(handle, data, max_retries=3)

    def _flush(self, handle: int) -> None:
        flush(handle)

    def _lock_volumes(self) -> list[int]:
        return [int(h) for h in lock_volumes(self.letters)]

    def _unlock_volumes(self, held: list[int]) -> None:
        unlock_volumes([ctypes.c_void_p(h) for h in held])

    def run(self) -> None:
        k32 = kernel32()
        k32.SetThreadExecutionState(
            ES_CONTINUOUS
            | ES_SYSTEM_REQUIRED
        )
        try:
            mode = diskpart.resolve_write_mode(
                self.write_mode, self.iso_path
            )
            self.mode.emit(mode)
            if mode == "filecopy":
                self._run_filecopy()
                if self._finished:
                    return
                if self.verify_after_write and (
                    self.verify_sha256 or self.bad_block_scan
                ):
                    # Filecopy mode writes a filesystem, not a raw image —
                    # byte-comparing against the ISO is meaningless.
                    self._verify_after_write(skip_source_iso=True)
                if self._finished:
                    return
                self.finished.emit(True, "")
                return
            self.phase.emit("Locking drive")
            volumes = self._lock_volumes()
            try:
                self.phase.emit("Writing")
                self._run_inner()
            finally:
                self._unlock_volumes(volumes)
            if self._finished:
                return
            if self.verify_after_write and (
                self.verify_sha256 or self.bad_block_scan
            ):
                self._verify_after_write()
            if self._finished:
                # A cancelled verification already reported its outcome;
                # never follow it with a (True, "") success signal.
                return
            self.finished.emit(True, "")
        except Exception as exc:
            logger.exception("UsbWriter.run failed")
            self.finished.emit(False, str(exc))
        finally:
            kernel32().SetThreadExecutionState(ES_CONTINUOUS)

    def _run_filecopy(self) -> None:
        """Repartition the drive, format it, then copy ISO contents."""
        if self._cancel_requested():
            self._finish_cancelled()
            return
        self.phase.emit("Preparing partition")
        letter = diskpart.prepare_partition(
            diskpart.drive_number_from_path(self.drive_path),
            self.partition_scheme,
            self.filesystem,
        )
        self.progress.emit(10.0)
        if self._cancel_requested():
            self._finish_cancelled()
            return
        if self.windows_to_go:
            self.phase.emit("Applying Windows image")
            diskpart.apply_windows_image(self.iso_path, letter)
        else:
            self.phase.emit("Copying files")
            diskpart.copy_iso_files(self.iso_path, letter)
        if self._cancel_requested():
            self._finish_cancelled()
            return
        if self.persistence and not self.windows_to_go:
            self.phase.emit("Creating persistence")
            paths = iso_mod.list_iso_paths(self.iso_path)
            ok, message = persistence.create_persistence(
                f"{letter}:\\", self.persistence_size_mb, paths
            )
            if ok:
                logger.info("persistence: %s", message)
            else:
                logger.warning("persistence partial: %s", message)
            self.note.emit(message)
        if self.bypass_tpm:
            self.phase.emit("Patching TPM bypass")
            from core.tpm_bypass import patch_boot_wim_on_usb

            patch_boot_wim_on_usb(letter)
            self.note.emit(
                "TPM / Secure Boot / RAM checks bypassed in boot.wim"
            )
        self.progress.emit(100.0)

    def _finish_cancelled(self) -> None:
        self._finished = True
        self.finished.emit(False, "cancelled")

    def _run_inner(self) -> None:
        handle = None
        try:
            total = os.path.getsize(self.iso_path)
            if total <= 0:
                raise ValueError("ISO file is empty")
            handle = self._open_drive()
            drive_size = self._drive_size(handle)
            if drive_size < total:
                self._finished = True
                self.finished.emit(
                    False, "drive is too small for this image"
                )
                return
        except Exception as exc:
            logger.exception("UsbWriter._run_inner: setup failed")
            self._finished = True
            self.finished.emit(False, str(exc))
            return
        finally:
            if handle is not None and self._finished:
                # Pre-flight failures return before the write loop; release
                # the drive handle here so Windows does not keep it busy.
                kernel32().CloseHandle(handle)
                handle = None

        assert handle is not None  # pre-flight failures returned above

        self.total_bytes.emit(total)

        if self.use_native and not self.resume:
            native_mod = _load_native_writer()
            if native_mod is not None:
                try:
                    self._run_native(native_mod, total)
                finally:
                    # The extension opens its own handles; this Python-side
                    # drive handle must still be released (it is only closed
                    # by the Python-path finally below).
                    kernel32().CloseHandle(handle)
                return

        written = 0
        source_written = 0
        manifest: jobs.JobManifest | None = None
        durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
        sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
        try:
            with open(self.iso_path, "rb") as source:
                if self.resume:
                    source_digest = jobs.source_sha256(self.iso_path)
                    options = {
                        "chunk_size": self.chunk_size,
                        "verify_after_write": self.verify_after_write,
                        "verify_sha256": self.verify_sha256,
                        "bad_block_scan": self.bad_block_scan,
                    }
                    if os.path.isfile(self.manifest_path):
                        manifest = jobs.load_manifest(self.manifest_path)
                        manifest.validate_resume(
                            source_path=self.iso_path,
                            source_size=total,
                            source_sha256=source_digest,
                            target_fingerprint=self.target_fingerprint,
                            target_size=drive_size,
                            options=options,
                        )
                    else:
                        manifest = jobs.JobManifest(
                            source_path=self.iso_path,
                            source_size=total,
                            source_sha256=source_digest,
                            target_fingerprint=self.target_fingerprint,
                            target_size=drive_size,
                            options=options,
                            state="writing",
                        )
                        jobs.save_manifest(self.manifest_path, manifest)
                    saved = manifest.checkpoint_bytes
                    if 0 < saved < total:
                        self._seek_drive(handle, saved)
                        source.seek(saved)
                        source_written = saved
                        self.note.emit(f"Resuming from byte {saved:,}")
                    elif saved == total:
                        raise ValueError("cannot resume: job is already complete")

                while chunk := source.read(self.chunk_size):
                    if self._cancel_requested():
                        break
                    # FILE_FLAG_NO_BUFFERING requires sector-aligned writes.
                    # Pad the final partial chunk with zeros (like dd).
                    source_chunk_len = len(chunk)
                    if source_chunk_len % self._SECTOR_SIZE != 0:
                        pad = self._SECTOR_SIZE - (source_chunk_len % self._SECTOR_SIZE)
                        chunk = chunk + b"\x00" * pad
                    chunk_start = time.perf_counter()
                    self._write_chunk(handle, chunk)
                    durations.append(time.perf_counter() - chunk_start)
                    sizes.append(len(chunk))
                    written += len(chunk)
                    source_written += source_chunk_len
                    if manifest is not None and (
                        source_written % (10 * 1024 * 1024) < self.chunk_size
                    ):
                        manifest.checkpoint_bytes = source_written
                        jobs.save_manifest(self.manifest_path, manifest)

                    window_bytes = sum(sizes)
                    window_time = sum(durations)
                    if window_time > 0 and window_bytes > 0:
                        bytes_per_sec = window_bytes / window_time
                        speed = bytes_per_sec / 1_000_000
                        remaining = (total - source_written) / bytes_per_sec
                    else:
                        speed = 0.0
                        remaining = 0.0

                    self.progress.emit(min(source_written / total * 100.0, 100.0))
                    self.speed_mbps.emit(speed)
                    self.written_bytes.emit(source_written)
                    self.eta_seconds.emit(int(remaining))
            if not self._cancel_requested():
                self.phase.emit("Flushing")
                self._flush(handle)
                if manifest is not None:
                    manifest.checkpoint_bytes = total
                    manifest.state = "written"
                    jobs.save_manifest(self.manifest_path, manifest)
                    try:
                        os.unlink(self.manifest_path)
                    except OSError:
                        pass
            elif manifest is not None:
                manifest.state = "resumable"
                manifest.error = "write cancelled"
                jobs.save_manifest(self.manifest_path, manifest)
        except OSError as exc:
            logger.exception("UsbWriter._run_inner: IO error")
            if manifest is not None:
                manifest.state = "resumable"
                manifest.error = str(exc)
                try:
                    jobs.save_manifest(self.manifest_path, manifest)
                except OSError:
                    logger.exception("failed to save resumable job manifest")
            self._finished = True
            self.finished.emit(False, str(exc))
            return
        finally:
            if handle is not None:
                kernel32().CloseHandle(handle)

        if self._cancel_requested():
            self._finished = True
            self.finished.emit(False, "cancelled")
            return

    def _run_native(self, native_mod: Any, total: int) -> None:
        """Raw write through the compiled native extension.

        The extension opens both files itself (CreateFile with
        FILE_FLAG_NO_BUFFERING); the volume locks taken by ``run()`` still
        apply. Progress is reported from the per-chunk callback.
        """
        written = 0
        durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
        sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
        last_done = 0
        last_time: float | None = None

        def on_progress(done: int, size: int) -> None:
            nonlocal written, last_done, last_time
            if self._cancel_requested():
                raise _NativeCancel("cancelled")
            now = time.perf_counter()
            if last_time is not None:
                durations.append(now - last_time)
                sizes.append(done - last_done)
            last_done = done
            last_time = now
            written = done
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

        self.phase.emit("Writing (native)")
        try:
            written = native_mod.native_write(
                self.iso_path, self.drive_path, self.chunk_size, on_progress
            )
        except _NativeCancel:
            self._finished = True
            self.finished.emit(False, "cancelled")
            return
        except OSError as exc:
            logger.exception("UsbWriter._run_native: IO error")
            self._finished = True
            self.finished.emit(False, str(exc))
            return
        self.written_bytes.emit(written)
        self.progress.emit(100.0)
        self.speed_mbps.emit(0.0)
        self.eta_seconds.emit(0)

    def _verify_after_write(self, skip_source_iso: bool = False) -> None:
        """Read the drive back and compare it against the source image.

        Byte-compares when ``verify_sha256`` is set (mismatch offsets are
        reported); the read-back SHA-256 is always computed and unreadable
        sectors are reported when ``bad_block_scan`` is set. Progress is
        reported through the regular progress signals.
        """
        t_start = time.perf_counter()
        last_done = 0
        last_time = t_start

        def on_progress(done: int, total: int) -> None:
            nonlocal last_done, last_time
            self.progress.emit(done / total * 100.0)
            self.written_bytes.emit(done)
            self.total_bytes.emit(total)
            now = time.perf_counter()
            dt = now - last_time
            if dt >= 0.5:
                dd = done - last_done
                speed = dd / dt / 1_000_000 if dt > 0 else 0.0
                self.speed_mbps.emit(speed)
                if speed > 0:
                    remaining = (total - done) / speed / 1_000_000
                    self.eta_seconds.emit(int(remaining))
                last_done = done
                last_time = now

        self.phase.emit("Verifying")
        result = verify_mod.verify_device(
            self.drive_path,
            source_iso=None if skip_source_iso else (self.iso_path if self.verify_sha256 else None),
            chunk_size=self.chunk_size,
            retries=self.bad_block_retries,
            progress=on_progress,
            is_cancelled=self._cancel_requested,
            scan_full_drive=self.bad_block_scan,
        )
        if result["error"] == "cancelled" or self._cancel_requested():
            # The write itself completed; only the verification was
            # cancelled. Report it as cancelled so the UI does not present
            # a false success and does not write a "verified" history entry.
            self._finished = True
            self.verify_result.emit(False, "cancelled", result)
            self.finished.emit(False, "cancelled")
            return
        message = self._verify_message(result)
        self.verify_result.emit(result["ok"], message, result)
        if not result["ok"]:
            # A failed verification is a failed flash. Consumers that only
            # listen to ``finished`` must never see a (True, "") success
            # after the write-back check reported problems.
            self._finished = True
            self.finished.emit(False, message)

    def _verify_message(self, result: dict[str, Any]) -> str:
        mismatches = len(result["mismatches"])
        bad = len(result["bad_sectors"])
        speed = result["speed_mbps"]
        if not result["ok"]:
            detail_parts = []
            if mismatches:
                detail_parts.append(f"{mismatches} mismatched region(s)")
            if bad:
                detail_parts.append(f"{bad} unreadable sector(s)")
            if not detail_parts:
                detail_parts.append(result["error"] or "verification failed")
            return (
                "verification failed: "
                + ", ".join(detail_parts)
                + f" (read-back at {speed:.1f} MB/s)"
            )
        return f"SHA-256 match, read-back at {speed:.1f} MB/s"
