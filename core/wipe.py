import ctypes
import hashlib
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
    verified = pyqtSignal(bool, str)
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
        verify: bool = True,
    ) -> None:
        super().__init__()
        self.drive_path = drive_path
        self.letters = letters or []
        self.method = method
        self.verify = verify
        # Per-run seed so the random pass is unrecoverable without it, yet
        # reproducible so the verification pass can read the drive back and
        # confirm the exact pattern that was written.
        self._seed = os.urandom(16)
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
        kernel32.ReadFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
        ]
        kernel32.SetFilePointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_ulong,
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
                self._unlock_volumes(held)
                raise OSError(
                    f"Volume {letter}: could not be opened for locking."
                )
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

    def _seek_start(self, handle: ctypes.c_void_p) -> None:
        self._kernel32().SetFilePointer(handle, 0, None, 0)

    def _read_chunk(self, handle: ctypes.c_void_p, size: int) -> bytes:
        kernel32 = self._kernel32()
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_ulong()
        ok = kernel32.ReadFile(
            handle,
            buffer,
            size,
            ctypes.byref(read),
            None,
        )
        if not ok:
            raise OSError(f"read failed: {ctypes.windll.kernel32.GetLastError()}")
        return buffer.raw[: read.value]

    def _pass_chunk(self, pattern: str, size: int, offset: int) -> bytes:
        if pattern == "zero":
            return b"\x00" * size
        if pattern == "ones":
            return b"\xff" * size
        return self._derive(offset, size)

    def _derive(self, offset: int, size: int) -> bytes:
        """Reproducible pseudo-random bytes for [offset, offset+size).

        A counter-mode stream over SHA-256 keyed by the per-run seed, so
        exactly the same bytes can be regenerated during verification.
        """
        out = bytearray()
        block = 32
        counter = offset // block
        over = offset % block
        while len(out) < over + size:
            key = self._seed + counter.to_bytes(8, "big")
            counter += 1
            out.extend(hashlib.sha256(key).digest())
        return bytes(out[over : over + size])

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
        self._verify_passed = True
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
        verify_total = total if self.verify else 0
        grand_total = total * passes + verify_total
        self.total_bytes.emit(grand_total)
        processed = 0
        durations: deque[float] = deque(maxlen=self.SPEED_WINDOW)
        sizes: deque[int] = deque(maxlen=self.SPEED_WINDOW)
        try:
            for pass_index, pattern in enumerate(patterns, 1):
                if self._canceled:
                    break
                if pass_index > 1:
                    self._seek_start(handle)
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
                        pattern, min(self.CHUNK_SIZE, total - done), done
                    )
                    chunk_start = time.perf_counter()
                    self._write_chunk(handle, chunk)
                    durations.append(time.perf_counter() - chunk_start)
                    sizes.append(len(chunk))
                    done += len(chunk)
                    processed += len(chunk)
                    self._emit_metrics(
                        processed, grand_total, durations, sizes
                    )
            if not self._canceled:
                self.phase.emit("Flushing")
                if not ctypes.windll.kernel32.FlushFileBuffers(handle):
                    raise OSError(
                        "flush failed: data may not have reached the drive"
                    )
            if not self._canceled and self.verify:
                self._verify_pass(
                    handle,
                    total,
                    patterns[-1],
                    processed,
                    grand_total,
                    durations,
                    sizes,
                )
            elif not self._canceled:
                self.verified.emit(True, "skipped")
        except OSError as exc:
            self.finished.emit(False, str(exc))
            return
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

        if self._canceled:
            self.finished.emit(False, "cancelled")
            return
        if not self._verify_passed:
            return  # _verify_pass already emitted the failure
        self.finished.emit(True, "")

    def _verify_pass(
        self,
        handle: ctypes.c_void_p,
        total: int,
        final_pattern: str,
        base_processed: int,
        grand_total: int,
        durations: deque[float],
        sizes: deque[int],
    ) -> None:
        """Read the drive back and confirm the final pass's pattern."""
        durations.clear()
        sizes.clear()
        self._seek_start(handle)
        self.phase.emit("Verifying wipe")
        done = 0
        try:
            while done < total:
                if self._canceled:
                    return
                size = min(self.CHUNK_SIZE, total - done)
                chunk_start = time.perf_counter()
                data = self._read_chunk(handle, size)
                durations.append(time.perf_counter() - chunk_start)
                sizes.append(size)
                if data != self._pass_chunk(final_pattern, size, done):
                    self._verify_passed = False
                    self.verified.emit(
                        False, f"data mismatch at offset {done}"
                    )
                    self.finished.emit(
                        False,
                        "verification failed: "
                        f"data mismatch at offset {done}",
                    )
                    return
                done += size
                self._emit_metrics(
                    base_processed + done, grand_total, durations, sizes
                )
        except OSError as exc:
            self._verify_passed = False
            self.verified.emit(False, "read error: " + str(exc))
            self.finished.emit(False, str(exc))
            return
        self.verified.emit(
            True,
            {
                "zero": "zeros confirmed",
                "ones": "ones confirmed",
                "random": "random pattern confirmed",
            }[final_pattern],
        )

    def _emit_metrics(
        self,
        processed: int,
        grand_total: int,
        durations: deque[float],
        sizes: deque[int],
    ) -> None:
        window_bytes = sum(sizes)
        window_time = sum(durations)
        if window_time > 0 and window_bytes > 0:
            bytes_per_sec = window_bytes / window_time
            speed = bytes_per_sec / 1_000_000
            remaining = (grand_total - processed) / bytes_per_sec
        else:
            speed = 0.0
            remaining = 0.0

        self.progress.emit(
            processed / grand_total * 100.0 if grand_total else 0.0
        )
        self.speed_mbps.emit(speed)
        self.written_bytes.emit(processed)
        self.eta_seconds.emit(int(remaining))
