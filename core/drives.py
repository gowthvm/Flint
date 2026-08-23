import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import psutil
import pythoncom
import wmi
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger("flint")


class DrivePoller(QThread):
    """Poll drive enumeration off the GUI thread. Emits drives_ready with
    each completed scan; loop with a small sleep until stop() is called."""

    drives_ready = pyqtSignal(list)

    def __init__(self, detector: "DriveDetector", interval_ms: int = 2000) -> None:
        super().__init__()
        self._detector = detector
        self._interval_ms = interval_ms
        self._scan_requested = False
        self._suspended = False

    def request_scan(self) -> None:
        self._scan_requested = True

    def suspend(self) -> None:
        """Pause polling while a drive is being written/verified/wiped.

        WMI enumeration during an active write is wasted work and can
        contend with the raw-device traffic; results are dropped by the UI
        while busy anyway.
        """
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False

    def run(self) -> None:
        pythoncom.CoInitialize()
        try:
            while not self.isInterruptionRequested():
                if self._suspended:
                    self.msleep(self._interval_ms)
                    continue
                try:
                    drives = self._detector.list_removable_drives()
                except Exception:
                    logger.exception("DrivePoller failed to list drives")
                    drives = []
                self.drives_ready.emit(drives)
                if self._scan_requested:
                    self._scan_requested = False
                    continue
                self.msleep(self._interval_ms)
        finally:
            pythoncom.CoUninitialize()


class DriveDetector:
    def __init__(self) -> None:
        self.last_error: str | None = None
        self._system_disk_cache: set[str] | None = None

    @staticmethod
    def format_size(num_bytes: float) -> str:
        gb = num_bytes / 1_000_000_000
        if gb >= 1:
            return f"{round(gb)} GB"
        mb = num_bytes / 1_000_000
        if mb >= 1:
            return f"{round(mb)} MB"
        return f"{num_bytes} B"

    @staticmethod
    def _is_removable(disk: Any) -> bool:
        media_type = getattr(disk, "MediaType", None) or ""
        if "removable" in media_type.lower():
            return True
        interface = getattr(disk, "InterfaceType", None) or ""
        return interface.upper() == "USB"

    def _drive_letters(self, disk: Any) -> list[str]:
        letters: list[str] = []
        try:
            for partition in disk.associators("Win32_DiskDriveToDiskPartition"):
                for logical in partition.associators(
                    "Win32_LogicalDiskToPartition"
                ):
                    caption = getattr(logical, "Caption", "")
                    if caption and caption[1:2] == ":":
                        letters.append(caption[0])
        except Exception:
            logger.exception("_drive_letters failed")
        return letters

    def _map_physical_paths(self, letters: list[str]) -> dict[str, str | None]:
        """Map drive letters to ``\\\\.\\PHYSICALDRIVEn`` paths in parallel."""
        result: dict[str, str | None] = {}
        if not letters:
            return result
        with ThreadPoolExecutor(max_workers=min(len(letters), 8)) as pool:
            futures = {
                pool.submit(self._physical_drive_for_letter, letter): letter
                for letter in letters
            }
            for future in as_completed(futures):
                letter = futures[future]
                try:
                    result[letter] = future.result()
                except Exception:
                    logger.exception("_map_physical_paths failed for %s", letter)
                    result[letter] = None
        return result

    def list_removable_drives(self) -> list[dict[str, Any]]:
        self._system_disk_cache = None
        wmi_error: Exception | None = None
        try:
            drives = self._list_with_wmi()
            if drives:
                self.last_error = None
                return drives
        except Exception as exc:
            wmi_error = exc
        try:
            drives = self._list_with_psutil()
            if drives:
                self.last_error = None
                return drives
        except Exception as exc:
            wmi_error = exc
        if wmi_error is not None:
            self.last_error = repr(wmi_error)
        else:
            self.last_error = None
        return []

    def _system_disk_paths(self) -> set[str]:
        """DeviceIDs of the disks hosting the OS drive (never flashable).

        A USB boot drive or a machine whose system disk also reports
        "removable" must never appear in the target list: flashing it would
        erase the running OS.  Result is cached per ``list_removable_drives``
        call to avoid redundant IOCTL round-trips.
        """
        if self._system_disk_cache is not None:
            return self._system_disk_cache
        system = (os.environ.get("SystemDrive") or "C:").strip()
        letter = system[0] if system else "C"
        physical = self._physical_drive_for_letter(letter)
        self._system_disk_cache = {physical} if physical else set()
        return self._system_disk_cache

    def _list_with_wmi(self) -> list[dict[str, Any]]:
        conn = wmi.WMI()
        result: list[dict[str, Any]] = []
        system_disks = self._system_disk_paths()
        for disk in conn.Win32_DiskDrive():
            if not self._is_removable(disk):
                continue
            device_id = getattr(disk, "DeviceID", "") or ""
            if device_id in system_disks:
                logger.info(
                    "skipping system disk %s in drive listing", device_id
                )
                continue
            letters = self._drive_letters(disk)
            size = getattr(disk, "Size", None)
            size_bytes = int(size) if size else 0
            result.append(
                {
                    "name": getattr(disk, "Caption", "") or "",
                    "letter": letters[0] if letters else "",
                    "letters": letters,
                    "size_gb": round(size_bytes / 1_000_000_000) if size_bytes else 0,
                    "bus_type": getattr(disk, "InterfaceType", "") or "USB",
                    "model": getattr(disk, "Model", "") or "",
                    "serial": getattr(disk, "SerialNumber", "") or "",
                    "physical_path": getattr(disk, "DeviceID", "") or "",
                }
            )
        return result

    @staticmethod
    def _physical_drive_for_letter(letter: str) -> str | None:
        """Map a drive letter to its ``\\\\.\\PHYSICALDRIVEn`` path.

        Uses IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS. Returns None when the
        volume spans multiple disks or the query fails — callers must skip
        such drives (fail closed) rather than write to a volume handle.
        """
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
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
        kernel32.DeviceIoControl.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_ulong

        class _DiskExtent(ctypes.Structure):
            _fields_ = [
                ("DiskNumber", ctypes.c_ulong),
                ("StartingOffset", ctypes.c_longlong),
                ("ExtentLength", ctypes.c_longlong),
            ]

        class _VolumeDiskExtents(ctypes.Structure):
            _fields_ = [
                ("NumberOfDiskExtents", ctypes.c_ulong),
                ("DiskExtents", _DiskExtent * 1),
            ]

        handle = kernel32.CreateFileW(
            f"\\\\.\\{letter}:",
            0x80000000,  # GENERIC_READ
            0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        try:
            extents = _VolumeDiskExtents()
            returned = ctypes.c_ulong()
            ok = kernel32.DeviceIoControl(
                handle,
                0x00560000,  # IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS
                None,
                0,
                ctypes.byref(extents),
                ctypes.sizeof(extents),
                ctypes.byref(returned),
                None,
            )
            if ok and extents.NumberOfDiskExtents == 1:
                return f"\\\\.\\PHYSICALDRIVE{extents.DiskExtents[0].DiskNumber}"
            return None
        finally:
            kernel32.CloseHandle(handle)

    def _list_with_psutil(self) -> list[dict[str, Any]]:
        import win32file

        removable: list[tuple[str, str, int]] = []  # (letter, mountpoint, device_type)
        for part in psutil.disk_partitions(all=False):
            letter = self._drive_letter_from_path(part.device)
            if letter is None:
                continue
            try:
                is_removable = (
                    win32file.GetDriveType(part.device)
                    == win32file.DRIVE_REMOVABLE
                )
            except Exception:
                logger.exception("win32file.GetDriveType failed")
                is_removable = False
            if not is_removable:
                continue
            try:
                size_bytes = shutil.disk_usage(part.mountpoint).total
            except Exception:
                logger.exception("shutil.disk_usage failed for %s", part.mountpoint)
                size_bytes = 0
            removable.append((letter, part.mountpoint, size_bytes))

        if not removable:
            return []

        letters = [letter for letter, _, _ in removable]
        path_map = self._map_physical_paths(letters)

        result: list[dict[str, Any]] = []
        for letter, mountpoint, size_bytes in removable:
            physical = path_map.get(letter)
            if physical is None:
                logger.warning(
                    "psutil fallback: skipping %s: no physical-drive mapping",
                    letter,
                )
                continue
            result.append(
                {
                    "name": f"Drive {letter}:",
                    "letter": letter,
                    "size_gb": round(size_bytes / 1_000_000_000) if size_bytes else 0,
                    "bus_type": "USB",
                    "model": f"USB Drive {letter}:",
                    "serial": "",
                    "letters": [letter],
                    "physical_path": physical,
                }
            )
        return result

    @staticmethod
    def _drive_letter_from_path(path: str) -> str | None:
        if len(path) >= 2 and path[1] == ":":
            return path[0]
        return None
