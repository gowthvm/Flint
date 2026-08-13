import ctypes
import hashlib
from collections.abc import Callable

from PyQt6.QtCore import QThread, pyqtSignal


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
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    CHUNK = 4 * 1024 * 1024

    handle = kernel32.CreateFileW(
        drive_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False, f"could not open drive for read-back: {drive_path}"
    try:
        if size is None:
            length = ctypes.c_ulonglong()
            returned = ctypes.c_ulong()
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL_DISK_GET_LENGTH_INFO,
                None,
                0,
                ctypes.byref(length),
                ctypes.sizeof(length),
                ctypes.byref(returned),
                None,
            )
            if not ok or length.value <= 0:
                return False, "could not determine drive size for verification"
            size = length.value
        digest = hashlib.sha256()
        remaining = size
        done = 0
        while remaining > 0:
            if is_cancelled is not None and is_cancelled():
                return False, "cancelled"
            count = min(CHUNK, remaining)
            buffer = ctypes.create_string_buffer(count)
            read = ctypes.c_ulong()
            ok = kernel32.ReadFile(
                handle,
                buffer,
                count,
                ctypes.byref(read),
                None,
            )
            if not ok or read.value == 0:
                return False, "drive read-back ended before the image"
            digest.update(buffer.raw[: read.value])
            remaining -= read.value
            done += read.value
            if progress is not None:
                progress(done, size)
        result = digest.hexdigest()
        if is_cancelled is not None and is_cancelled():
            return False, "cancelled"
        if expected_sha256 is not None and result != expected_sha256:
            return False, "verification failed: hash mismatch"
        return True, result
    finally:
        kernel32.CloseHandle(handle)


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
        ok, result = hash_drive(
            self._drive_path,
            self._size,
            self._expected_sha256,
            progress=lambda done, total: (
                self.progress.emit(done / total * 100.0),
                self.stats.emit(done, total),
            ),
            is_cancelled=lambda: self._cancelled,
        )
        if not ok:
            self.finished.emit(False, result)
            return
        self.progress.emit(100.0)
        self.finished.emit(True, result)