import ctypes
import logging
import os
import time
from collections import deque

from PyQt6.QtCore import QThread, pyqtSignal

from core import diskpart, persistence
from core import iso as iso_mod

logger = logging.getLogger("flint")


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
    finished = pyqtSignal(bool, str)

    CHUNK_SIZE = 4 * 1024 * 1024
    SPEED_WINDOW = 5

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _OPEN_EXISTING = 3
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    _FSCTL_DISMOUNT_VOLUME = 0x00090020
    _FSCTL_LOCK_VOLUME = 0x00090018
    _FSCTL_UNLOCK_VOLUME = 0x0009001C
    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001
    _ES_DISPLAY_REQUIRED = 0x00000002

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
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    def _open_drive(self) -> ctypes.c_void_p:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            self.drive_path,
            self._GENERIC_READ | self._GENERIC_WRITE,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            None,
            self._OPEN_EXISTING,
            0,
            None,
        )
        if not handle or handle == self._INVALID_HANDLE_VALUE:
            raise OSError(f"drive not writable: {self.drive_path}")
        return handle

    def _drive_size(self, handle: ctypes.c_void_p) -> int:
        kernel32 = ctypes.windll.kernel32
        size = ctypes.c_ulonglong()
        returned = ctypes.c_ulong()
        ok = kernel32.DeviceIoControl(
            handle,
            self._IOCTL_DISK_GET_LENGTH_INFO,
            None,
            0,
            ctypes.byref(size),
            ctypes.sizeof(size),
            ctypes.byref(returned),
            None,
        )
        if not ok:
            raise OSError("failed to query drive size")
        return size.value

    def _write_chunk(self, handle: ctypes.c_void_p, data: bytes) -> None:
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_string_buffer(data)
        written = ctypes.c_ulong()
        ok = kernel32.WriteFile(
            handle,
            buffer,
            len(data),
            ctypes.byref(written),
            None,
        )
        if not ok:
            raise OSError(f"write failed: {ctypes.windll.kernel32.GetLastError()}")
        if written.value != len(data):
            raise OSError("short write on drive")

    def _flush(self, handle: ctypes.c_void_p) -> None:
        if not ctypes.windll.kernel32.FlushFileBuffers(handle):
            raise OSError(
                "flush failed: data may not have reached the drive"
            )

    def _device_control(self, handle: ctypes.c_void_p, code: int) -> bool:
        kernel32 = ctypes.windll.kernel32
        returned = ctypes.c_ulong()
        return bool(
            kernel32.DeviceIoControl(
                handle,
                code,
                None,
                0,
                None,
                0,
                ctypes.byref(returned),
                None,
            )
        )

    def _lock_volumes(self) -> list[ctypes.c_void_p]:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        _GENERIC_READ = 0x80000000
        _GENERIC_WRITE = 0x40000000
        _OPEN_EXISTING = 3
        held: list[ctypes.c_void_p] = []
        for letter in self.letters:
            path = f"\\\\.\\{letter}:"
            handle = kernel32.CreateFileW(
                path,
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                0,
                None,
            )
            if not handle or handle == self._INVALID_HANDLE_VALUE:
                continue
            self._device_control(handle, self._FSCTL_DISMOUNT_VOLUME)
            locked = False
            for _ in range(5):
                if self._device_control(handle, self._FSCTL_LOCK_VOLUME):
                    locked = True
                    break
                time.sleep(0.2)
            if not locked:
                kernel32.CloseHandle(handle)
                self._unlock_volumes(held)
                raise OSError(
                    f"Volume {letter}: is in use by another program. "
                    "Close it and try again."
                )
            held.append(handle)
        return held

    def _unlock_volumes(self, held: list[ctypes.c_void_p]) -> None:
        kernel32 = ctypes.windll.kernel32
        for handle in held:
            self._device_control(handle, self._FSCTL_UNLOCK_VOLUME)
            kernel32.CloseHandle(handle)

    def run(self) -> None:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState(
            self._ES_CONTINUOUS
            | self._ES_SYSTEM_REQUIRED
            | self._ES_DISPLAY_REQUIRED
        )
        try:
            mode = diskpart.resolve_write_mode(
                self.write_mode, self.iso_path
            )
            self.mode.emit(mode)
            if mode == "filecopy":
                self._run_filecopy()
                return
            self.phase.emit("Locking drive")
            volumes = self._lock_volumes()
            try:
                self.phase.emit("Writing")
                self._run_inner()
            finally:
                self._unlock_volumes(volumes)
        except Exception as exc:
            logger.exception("UsbWriter.run failed")
            self.finished.emit(False, str(exc))
        finally:
            kernel32.SetThreadExecutionState(self._ES_CONTINUOUS)

    def _run_filecopy(self) -> None:
        """Repartition the drive, format it, then copy ISO contents."""
        self.phase.emit("Preparing partition")
        letter = diskpart.prepare_partition(
            diskpart.drive_number_from_path(self.drive_path),
            self.partition_scheme,
            self.filesystem,
        )
        self.progress.emit(10.0)
        if self.windows_to_go:
            self.phase.emit("Applying Windows image")
            diskpart.apply_windows_image(self.iso_path, letter)
        else:
            self.phase.emit("Copying files")
            diskpart.copy_iso_files(self.iso_path, letter)
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
        self.progress.emit(100.0)
        self.finished.emit(True, "")

    def _run_inner(self) -> None:
        try:
            total = os.path.getsize(self.iso_path)
            if total <= 0:
                raise ValueError("ISO file is empty")
            handle = self._open_drive()
            try:
                drive_size = self._drive_size(handle)
                if drive_size < total:
                    self.finished.emit(
                        False, "drive is too small for this image"
                    )
                    return
            except OSError as exc:
                self.finished.emit(False, str(exc))
                return
        except Exception as exc:
            logger.exception("UsbWriter._run_inner: setup failed")
            self.finished.emit(False, str(exc))
            return

        self.total_bytes.emit(total)

        written = 0
        durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
        sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
        try:
            with open(self.iso_path, "rb") as source:
                while chunk := source.read(self.CHUNK_SIZE):
                    if self._canceled:
                        break
                    chunk_start = time.perf_counter()
                    self._write_chunk(handle, chunk)
                    durations.append(time.perf_counter() - chunk_start)
                    sizes.append(len(chunk))
                    written += len(chunk)

                    window_bytes = sum(sizes)
                    window_time = sum(durations)
                    if window_time > 0 and window_bytes > 0:
                        bytes_per_sec = window_bytes / window_time
                        speed = bytes_per_sec / 1_000_000
                        remaining = (total - written) / bytes_per_sec
                    else:
                        speed = 0.0
                        remaining = 0.0

                    self.progress.emit(written / total * 100.0)
                    self.speed_mbps.emit(speed)
                    self.written_bytes.emit(written)
                    self.eta_seconds.emit(int(remaining))
            if not self._canceled:
                self.phase.emit("Flushing")
                self._flush(handle)
        except OSError as exc:
            logger.exception("UsbWriter._run_inner: IO error")
            self.finished.emit(False, str(exc))
            return
        finally:
            if handle is not None:
                ctypes.windll.kernel32.CloseHandle(handle)

        if self._canceled:
            self.finished.emit(False, "cancelled")
            return
        self.finished.emit(True, "")