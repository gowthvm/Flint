import shutil
from typing import Any

import psutil
import wmi
from PyQt6.QtCore import QThread, pyqtSignal
import logging

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

    def request_scan(self) -> None:
        self._scan_requested = True

    def run(self) -> None:
        while not self.isInterruptionRequested():
            try:
                drives = self._detector.list_removable_drives()
            except Exception as exc:
                logger.exception("DrivePoller failed to list drives")
                drives = []
            self.drives_ready.emit(drives)
            if self._scan_requested:
                self._scan_requested = False
                continue
            self.msleep(self._interval_ms)


class DriveDetector:
    def __init__(self) -> None:
        self.last_error: str | None = None

    @staticmethod
    def format_size(num_bytes: float | int) -> str:
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
        except Exception as exc:
            logger.exception("_drive_letters failed")
        return letters

    def list_removable_drives(self) -> list[dict]:
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

    def _list_with_wmi(self) -> list[dict]:
        conn = wmi.WMI()
        result: list[dict] = []
        for disk in conn.Win32_DiskDrive():
            if not self._is_removable(disk):
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

    def _list_with_psutil(self) -> list[dict]:
        import win32file

        result: list[dict] = []
        for part in psutil.disk_partitions(all=False):
            letter = self._drive_letter_from_path(part.device)
            if letter is None:
                continue
            try:
                removable = (
                    win32file.GetDriveType(part.device)
                    == win32file.DRIVE_REMOVABLE
                )
            except Exception as exc:
                logger.exception("win32file.GetDriveType failed")
                removable = False
            if not removable:
                continue
            try:
                size_bytes = shutil.disk_usage(part.mountpoint).total
            except Exception as exc:
                logger.exception("shutil.disk_usage failed for %s", part.mountpoint)
                size_bytes = 0
            result.append(
                {
                    "name": f"Drive {letter}:",
                    "letter": letter,
                    "size_gb": round(size_bytes / 1_000_000_000) if size_bytes else 0,
                    "bus_type": "USB",
                    "model": f"USB Drive {letter}:",
                    "serial": "",
                    "letters": [letter],
                    "physical_path": part.device,
                }
            )
        return result

    @staticmethod
    def _drive_letter_from_path(path: str) -> str | None:
        if len(path) >= 2 and path[1] == ":":
            return path[0]
        return None