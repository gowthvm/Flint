import ctypes
import time
from collections import deque

from PyQt6.QtCore import QThread, pyqtSignal
import logging

logger = logging.getLogger("flint")


class WipeWorker(QThread):
    """Zero-fill an entire raw drive, with progress, rolling speed and
    cancellation support."""

    progress = pyqtSignal(float)
    speed_mbps = pyqtSignal(float)
    written_bytes = pyqtSignal(int)
    total_bytes = pyqtSignal(int)
    eta_seconds = pyqtSignal(int)
    phase = pyqtSignal(str)
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
        self, drive_path: str, letters: list[str] | None = None
    ) -> None:
        super().__init__()
        self.drive_path = drive_path
        self.letters = letters or []
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

    def run(self) -> None:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState(
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
            logger.exception("WipeWorker.run failed")
            self.finished.emit(False, str(exc))
        finally:
            kernel32.SetThreadExecutionState(self._ES_CONTINUOUS)

    def _run_inner(self) -> None:
        handle = self._open_drive()
        try:
            total = self._drive_size(handle)
            if total <= 0:
                raise OSError("unable to determine drive size")
            zeros = b"\x00" * self.CHUNK_SIZE
        except OSError as exc:
            self.finished.emit(False, str(exc))
            ctypes.windll.kernel32.CloseHandle(handle)
            return

        self.total_bytes.emit(total)
        written = 0
        durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
        sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
        try:
            self.phase.emit("Wiping")
            while written < total:
                if self._canceled:
                    break
                chunk = zeros[: min(self.CHUNK_SIZE, total - written)]
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
                if not ctypes.windll.kernel32.FlushFileBuffers(handle):
                    raise OSError(
                        "flush failed: data may not have reached the drive"
                    )
        except OSError as exc:
            self.finished.emit(False, str(exc))
            return
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

        if self._canceled:
            self.finished.emit(False, "cancelled")
            return
        self.finished.emit(True, "")