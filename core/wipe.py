import ctypes
import logging
import os
import time
from collections import deque
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger("flint")

# Wipe methods and their pass patterns (in order):
#   zero   - single pass of zeros (fast; basic erasure)
#   random - single pass of random data (NIST SP 800-88 clear equivalent)
#   nist   - alias for "random"
#   dod    - DoD 5220.22-M style: zeros, then ones, then random data
WIPE_METHODS = ("zero", "random", "nist", "dod")


def _wipe_patterns(method: str) -> list[str]:
    if method == "zero":
        return ["zero"]
    if method in ("random", "nist"):
        return ["random"]
    if method == "dod":
        return ["zero", "ones", "random"]
    raise ValueError(f"unknown wipe method: {method!r}")


class WipeWorker(QThread):
    """Overwrite an entire raw drive with a configurable pass pattern, with
    progress, rolling speed and cancellation support."""

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
        self,
        drive_path: str,
        letters: list[str] | None = None,
        method: str = "zero",
    ) -> None:
        super().__init__()
        self.drive_path = drive_path
        self.letters = letters or []
        self.method = method
        # Validate eagerly so a typo never silently defaults to zero-fill.
        _wipe_patterns(method)
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    def _open_drive(self) -> ctypes.c_void_p:
        kernel32 = self._kernel32()
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
        return ctypes.c_void_p(handle)

    @staticmethod
    def _kernel32() -> Any:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
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
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.WriteFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
        ]
        kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        kernel32.SetThreadExecutionState.argtypes = [ctypes.c_ulong]
        kernel32.SetThreadExecutionState.restype = ctypes.c_ulong
        return kernel32

    def _drive_size(self, handle: ctypes.c_void_p) -> int:
        kernel32 = self._kernel32()
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
        kernel32 = self._kernel32()
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
        kernel32 = self._kernel32()
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
        kernel32 = self._kernel32()
        for handle in held:
            self._device_control(handle, self._FSCTL_UNLOCK_VOLUME)
            kernel32.CloseHandle(handle)

    def _write_chunk(self, handle: ctypes.c_void_p, data: bytes) -> None:
        kernel32 = self._kernel32()
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

    @staticmethod
    def _pass_chunk(pattern: str, size: int) -> bytes:
        if pattern == "zero":
            return b"\x00" * size
        if pattern == "ones":
            return b"\xff" * size
        return os.urandom(size)

    def run(self) -> None:
        kernel32 = self._kernel32()
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
            patterns = _wipe_patterns(self.method)
        except OSError as exc:
            self.finished.emit(False, str(exc))
            ctypes.windll.kernel32.CloseHandle(handle)
            return

        passes = len(patterns)
        # Every pass writes the whole drive: report honest totals so the
        # written/expected ratio and progress stay truthful across passes.
        self.total_bytes.emit(total * passes)
        written = 0
        durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
        sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
        try:
            for pass_index, pattern in enumerate(patterns, 1):
                if self._canceled:
                    break
                self.phase.emit(
                    f"Wiping pass {pass_index}/{passes}"
                    if passes > 1
                    else "Wiping"
                )
                done = 0
                while done < total:
                    if self._canceled:
                        break
                    chunk = self._pass_chunk(
                        pattern, min(self.CHUNK_SIZE, total - done)
                    )
                    chunk_start = time.perf_counter()
                    self._write_chunk(handle, chunk)
                    durations.append(time.perf_counter() - chunk_start)
                    sizes.append(len(chunk))
                    done += len(chunk)
                    written += len(chunk)

                    window_bytes = sum(sizes)
                    window_time = sum(durations)
                    if window_time > 0 and window_bytes > 0:
                        bytes_per_sec = window_bytes / window_time
                        speed = bytes_per_sec / 1_000_000
                        remaining = (total * passes - written) / bytes_per_sec
                    else:
                        speed = 0.0
                        remaining = 0.0

                    self.progress.emit(written / (total * passes) * 100.0)
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