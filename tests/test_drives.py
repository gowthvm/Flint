"""Drive-detection tests: the letter-to-PHYSICALDRIVE mapping must fail
closed (never hand out a volume handle for a destructive write) and the
psutil fallback must skip drives it cannot map.

The IOCTL path is exercised with fake kernel32 functions so no real
hardware or volume is touched.
"""

import ctypes
import sys
import types

from core import drives


def _patch_kernel32(monkeypatch, *, open_ok=True, n_extents=1, ioctl_ok=True,
                    disk_number=3):
    """Monkeypatch kernel32 with plain closure functions.

    The code under test assigns ``.argtypes`` / ``.restype`` onto the
    kernel32 attributes it finds; bound methods have no ``__dict__``, so the
    fakes must be plain functions.
    """
    opened_paths: list[str] = []
    closed: list = []

    def create_file_w(path, access, share, sec, disp, flags, template):
        opened_paths.append(path)
        if not open_ok:
            return ctypes.c_void_p(0)
        return ctypes.c_void_p(0x1234)

    def device_io_control(handle, code, inbuf, inlen, outbuf, outlen,
                          returned, overlapped):
        if not ioctl_ok:
            return 0
        # ``outbuf`` is the byref() CArgObject; unwrap the real struct.
        struct = outbuf._obj
        struct.NumberOfDiskExtents = n_extents
        if n_extents >= 1:
            struct.DiskExtents[0].DiskNumber = disk_number
        returned._obj.value = 16
        return 1

    def close_handle(handle):
        closed.append(handle)
        return 1

    kernel32 = ctypes.windll.kernel32
    monkeypatch.setattr(kernel32, "CreateFileW", create_file_w)
    monkeypatch.setattr(kernel32, "DeviceIoControl", device_io_control)
    monkeypatch.setattr(kernel32, "CloseHandle", close_handle)
    return type(
        "Fake", (), {"opened_paths": opened_paths, "closed": closed}
    )()


# -------------------------------------------------- letter -> physical disk --


def test_physical_drive_single_extent(monkeypatch):
    fake = _patch_kernel32(monkeypatch, n_extents=1, disk_number=3)
    path = drives.DriveDetector._physical_drive_for_letter("E")
    assert path == r"\\.\PHYSICALDRIVE3"
    assert fake.opened_paths == [r"\\.\E:"]
    assert len(fake.closed) == 1


def test_physical_drive_multiple_extents_is_rejected(monkeypatch):
    fake = _patch_kernel32(monkeypatch, n_extents=2)
    # A volume spanning several disks has no single physical drive.
    assert drives.DriveDetector._physical_drive_for_letter("E") is None
    assert len(fake.closed) == 1


def test_physical_drive_ioctl_failure_is_rejected(monkeypatch):
    fake = _patch_kernel32(monkeypatch, ioctl_ok=False)
    assert drives.DriveDetector._physical_drive_for_letter("E") is None
    assert len(fake.closed) == 1


def test_physical_drive_open_failure(monkeypatch):
    fake = _patch_kernel32(monkeypatch, open_ok=False)
    assert drives.DriveDetector._physical_drive_for_letter("E") is None
    assert fake.closed == []


# ------------------------------------------------- psutil fallback path -----


def test_psutil_fallback_skips_unmapped_drive(monkeypatch):
    """F5-01 regression: when the letter cannot be mapped to a physical
    drive the psutil fallback must skip it entirely — never hand out a
    volume path (\\\\.\\E:) that would corrupt one partition."""
    part = types.SimpleNamespace(device="E:\\", mountpoint="E:\\")
    monkeypatch.setattr(
        drives.psutil, "disk_partitions", lambda all=False: [part]
    )

    class _FakeWin32File:
        DRIVE_REMOVABLE = 2

        @staticmethod
        def GetDriveType(device):
            return 2

    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    monkeypatch.setattr(
        drives.DriveDetector, "_physical_drive_for_letter",
        staticmethod(lambda letter: None),
    )

    result = drives.DriveDetector()._list_with_psutil()

    assert result == []


def test_psutil_fallback_uses_physical_path(monkeypatch):
    part = types.SimpleNamespace(device="E:\\", mountpoint="E:\\")
    monkeypatch.setattr(
        drives.psutil, "disk_partitions", lambda all=False: [part]
    )

    class _FakeWin32File:
        DRIVE_REMOVABLE = 2

        @staticmethod
        def GetDriveType(device):
            return 2

    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    monkeypatch.setattr(
        drives.DriveDetector, "_physical_drive_for_letter",
        staticmethod(lambda letter: r"\\.\PHYSICALDRIVE1"),
    )
    monkeypatch.setattr(
        drives.shutil, "disk_usage",
        lambda mountpoint: types.SimpleNamespace(total=32_000_000_000),
    )

    result = drives.DriveDetector()._list_with_psutil()

    assert len(result) == 1
    assert result[0]["letter"] == "E"
    assert result[0]["physical_path"] == r"\\.\PHYSICALDRIVE1"
    assert not result[0]["physical_path"].startswith(r"\\.\E:")
