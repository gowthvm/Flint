"""Drive-detection tests: the letter-to-PHYSICALDRIVE mapping must fail
closed (never hand out a volume handle for a destructive write), the
psutil fallback must skip drives it cannot map and must exclude the OS
disk (B02), detection must fail closed when the OS disk cannot be
identified (B01), SystemDrive must be sanitized (B15), and the psutil
path must merge a stick's partitions (L15b) and publish the disk size
from the IOCTL rather than a volume size (L15a).

The IOCTL path is exercised with fake kernel32 functions so no real
hardware or volume is touched.
"""

import ctypes
import logging
import sys
import types

import pytest

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


# ------------------------------------------------------------- psutil helpers ---

_D0 = r"\\.\PHYSICALDRIVE0"
_D1 = r"\\.\PHYSICALDRIVE1"


class _FakeWin32File:
    DRIVE_REMOVABLE = 2

    @staticmethod
    def GetDriveType(device):
        return 2


def _set_partitions(monkeypatch, *letters: str) -> None:
    parts = [
        types.SimpleNamespace(device=f"{letter}:\\", mountpoint=f"{letter}:\\")
        for letter in letters
    ]
    monkeypatch.setattr(
        drives.psutil, "disk_partitions", lambda all=False: parts
    )


def _set_letter_map(monkeypatch, mapping: dict[str, str | None]) -> None:
    """Patch the letter→physical mapping; unmapped letters map to None."""
    monkeypatch.setattr(
        drives.DriveDetector,
        "_physical_drive_for_letter",
        staticmethod(lambda letter: mapping.get(letter)),
    )


def _set_disk_usage(monkeypatch, total: int) -> None:
    monkeypatch.setattr(
        drives.shutil,
        "disk_usage",
        lambda mountpoint: types.SimpleNamespace(total=total),
    )


def _patch_size_ioctl(monkeypatch, *, size: int = 0, fail: bool = False):
    """Mock core.deviceio's open_drive/drive_size so no real disk is opened.

    Simpler than faking kernel32: the drive-size IOCTL uses a different
    struct than the letter-mapping fake above.
    """
    from core import deviceio

    opened: list[str] = []
    write_flags: list[bool] = []
    closed: list[int] = []

    def fake_open(path, *, write, flags=0, exclusive=False):
        opened.append(path)
        write_flags.append(write)
        return 0x1234

    def fake_drive_size(handle):
        if fail:
            raise OSError("could not determine drive size")
        return size

    fake_k32 = types.SimpleNamespace(
        CloseHandle=lambda handle: closed.append(handle) or 1
    )
    monkeypatch.setattr(deviceio, "open_drive", fake_open)
    monkeypatch.setattr(deviceio, "drive_size", fake_drive_size)
    monkeypatch.setattr(deviceio, "kernel32", lambda: fake_k32)
    return types.SimpleNamespace(
        opened=opened, write_flags=write_flags, closed=closed
    )


def _set_system_drive(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SystemDrive", value)


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
    _set_system_drive(monkeypatch, "C:")
    _set_partitions(monkeypatch, "E")
    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    # C: maps fine (so system-disk identification succeeds) while E: has
    # no physical mapping and must be skipped.
    _set_letter_map(monkeypatch, {"C": _D0})
    _set_disk_usage(monkeypatch, total=32_000_000_000)
    _patch_size_ioctl(monkeypatch, size=32_000_000_000)

    result = drives.DriveDetector()._list_with_psutil()

    assert result == []


def test_psutil_fallback_uses_physical_path(monkeypatch):
    _set_system_drive(monkeypatch, "C:")
    _set_partitions(monkeypatch, "E")
    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    _set_letter_map(monkeypatch, {"C": _D0, "E": _D1})
    _set_disk_usage(monkeypatch, total=32_000_000_000)
    # Exact disk size unavailable → L15 fallback to the volume total.
    _patch_size_ioctl(monkeypatch, fail=True)

    result = drives.DriveDetector()._list_with_psutil()

    assert len(result) == 1
    assert result[0]["letter"] == "E"
    assert result[0]["physical_path"] == _D1
    assert not result[0]["physical_path"].startswith(r"\\.\E:")
    assert result[0]["size_bytes"] == 32_000_000_000
    assert result[0]["size_gb"] == 32


def test_psutil_path_excludes_system_physical_disk(monkeypatch):
    """B02 regression: the psutil fallback must apply the same system-disk
    exclusion as the WMI path — a removable-volume OS boot disk (Windows
    To Go) on E: must never be listed as flashable."""
    _set_system_drive(monkeypatch, "C:")
    _set_partitions(monkeypatch, "E", "F")
    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    _set_letter_map(
        monkeypatch, {"C": _D0, "E": _D0, "F": _D1}  # E: lives on the OS disk
    )
    _set_disk_usage(monkeypatch, total=16_000_000_000)
    ioctl = _patch_size_ioctl(monkeypatch, size=16_000_000_000)

    result = drives.DriveDetector()._list_with_psutil()

    assert [d["letter"] for d in result] == ["F"]
    assert result[0]["physical_path"] == _D1
    # The excluded disk is skipped before its size is ever queried.
    assert ioctl.opened == [_D1]


def test_psutil_merges_partitions_of_one_physical_stick(monkeypatch):
    """L15(b): a two-partition stick must produce ONE record whose letters
    are the union (first letter stays primary), sized from the disk."""
    _set_system_drive(monkeypatch, "C:")
    _set_partitions(monkeypatch, "E", "F")
    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    _set_letter_map(monkeypatch, {"C": _D0, "E": _D1, "F": _D1})
    _set_disk_usage(monkeypatch, total=16_000_000_000)
    ioctl = _patch_size_ioctl(monkeypatch, size=32_000_000_000)

    result = drives.DriveDetector()._list_with_psutil()

    assert len(result) == 1
    record = result[0]
    assert record["physical_path"] == _D1
    assert record["letter"] == "E"
    assert record["letters"] == ["E", "F"]
    assert record["name"] == "Drive E:"
    assert record["size_bytes"] == 32_000_000_000
    assert record["size_gb"] == 32
    # One read-only IOCTL per physical disk, not per partition.
    assert ioctl.opened == [_D1]
    assert ioctl.write_flags == [False]
    assert ioctl.closed == [0x1234]


def test_psutil_size_comes_from_disk_ioctl(monkeypatch):
    """L15(a): size_bytes must be the whole-disk size from
    IOCTL_DISK_GET_LENGTH_INFO, not the volume's size."""
    _set_system_drive(monkeypatch, "C:")
    _set_partitions(monkeypatch, "E")
    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    _set_letter_map(monkeypatch, {"C": _D0, "E": _D1})
    _set_disk_usage(monkeypatch, total=32_000_000_000)  # volume size (ignored)
    ioctl = _patch_size_ioctl(monkeypatch, size=64_000_000_000)

    result = drives.DriveDetector()._list_with_psutil()

    assert result[0]["size_bytes"] == 64_000_000_000
    assert result[0]["size_gb"] == 64
    assert ioctl.opened == [_D1]
    assert ioctl.write_flags == [False]
    assert ioctl.closed == [0x1234]


def test_psutil_size_falls_back_to_volume_on_oserror(monkeypatch):
    """L15(a): when the disk-size IOCTL fails the volume total is used —
    and the handle (if opened) is always closed."""
    from core import deviceio

    _set_system_drive(monkeypatch, "C:")
    _set_partitions(monkeypatch, "E")
    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    _set_letter_map(monkeypatch, {"C": _D0, "E": _D1})
    _set_disk_usage(monkeypatch, total=32_000_000_000)
    ioctl = _patch_size_ioctl(monkeypatch, fail=True)

    result = drives.DriveDetector()._list_with_psutil()

    assert result[0]["size_bytes"] == 32_000_000_000
    assert result[0]["size_gb"] == 32
    assert ioctl.closed == [0x1234]

    # open_drive itself failing (access denied) also falls back — and
    # never attempts to close a handle that was never obtained.
    def raising_open(path, *, write, flags=0, exclusive=False):
        raise OSError("access denied")

    monkeypatch.setattr(deviceio, "open_drive", raising_open)
    result2 = drives.DriveDetector()._list_with_psutil()

    assert result2[0]["size_bytes"] == 32_000_000_000
    assert ioctl.opened == [_D1]
    assert ioctl.closed == [0x1234]


# --------------------------------------------- system-disk identification ----


def test_system_disk_mapping_failure_fails_closed(monkeypatch):
    """B01 (P0): when the SystemDrive letter cannot be mapped to a physical
    disk, detection must fail closed — no enumeration at all, last_error
    set, and `_system_disk_paths()` reporting None (never set())."""
    _set_system_drive(monkeypatch, "C:")
    _set_letter_map(monkeypatch, {})  # every letter fails to map

    wmi_calls: list[str] = []
    monkeypatch.setattr(
        drives.wmi, "WMI", lambda: wmi_calls.append("WMI()")
    )

    det = drives.DriveDetector()
    result = det.list_removable_drives()

    assert result == []
    assert det.last_error is not None
    assert "system disk" in det.last_error.lower()
    assert wmi_calls == []  # never enumerated drives at all
    # Contract: None = system disk could NOT be determined (cached as such).
    assert det._system_disk_paths() is None

    # A raising mapping (IOCTL crash) is treated the same way.
    def boom(letter):
        raise OSError("ioctl exploded")

    monkeypatch.setattr(
        drives.DriveDetector, "_physical_drive_for_letter", staticmethod(boom)
    )
    det2 = drives.DriveDetector()
    assert det2.list_removable_drives() == []
    assert det2.last_error is not None
    assert "system disk" in det2.last_error.lower()


def test_system_disk_paths_cached_within_scan(monkeypatch):
    """The None/set result must be cached per scan (one mapping round-trip),
    and the cache reset at the top of each list_removable_drives() call."""
    _set_system_drive(monkeypatch, "D:")
    calls: list[str] = []
    # B15 extras (pagefile/boot volumes) have their own test; stub them
    # out here so the call-count assertions see only the OS-disk mapping.
    monkeypatch.setattr(
        drives.DriveDetector, "_other_system_disk_paths", lambda self: set()
    )

    def fake_map(letter):
        calls.append(letter)
        return _D0

    monkeypatch.setattr(
        drives.DriveDetector,
        "_physical_drive_for_letter",
        staticmethod(fake_map),
    )

    det = drives.DriveDetector()
    assert det._system_disk_paths() == {_D0}
    assert det._system_disk_paths() == {_D0}
    assert calls == ["D"]  # resolved once, then served from cache

    # The cache is reset at the top of every list_removable_drives() call,
    # so the next scan resolves exactly once more (empty scans are fine).
    monkeypatch.setitem(sys.modules, "win32file", _FakeWin32File)
    _set_partitions(monkeypatch)
    monkeypatch.setattr(
        drives.wmi,
        "WMI",
        lambda: types.SimpleNamespace(Win32_DiskDrive=list),
    )
    assert det.list_removable_drives() == []
    assert det.last_error is None
    assert det._system_disk_paths() == {_D0}
    assert calls == ["D", "D"]


def test_enumeration_helpers_refuse_without_system_disk(monkeypatch):
    """Defense in depth: both enumeration paths raise (fail closed) when
    called directly while the system disk is undetermined."""
    _set_system_drive(monkeypatch, "C:")
    _set_letter_map(monkeypatch, {})  # every letter fails to map

    det = drives.DriveDetector()
    with pytest.raises(RuntimeError, match="system disk"):
        det._list_with_wmi()
    with pytest.raises(RuntimeError, match="system disk"):
        det._list_with_psutil()


def test_system_drive_env_sanitized(monkeypatch, caplog):
    """B15: SystemDrive is validated — only a stripped 'X:' value is
    trusted; anything else falls back to C: and is logged."""
    monkeypatch.setenv("SystemDrive", "banana;")
    with caplog.at_level(logging.WARNING, logger="flint"):
        assert drives.DriveDetector._system_drive_letter() == "C"
    assert "SystemDrive" in caplog.text

    monkeypatch.setenv("SystemDrive", "  D:  ")
    assert drives.DriveDetector._system_drive_letter() == "D"

    # The sanitized letter is what actually gets mapped.
    seen: list[str] = []
    monkeypatch.setattr(
        drives.DriveDetector, "_other_system_disk_paths", lambda self: set()
    )
    monkeypatch.setattr(
        drives.DriveDetector,
        "_physical_drive_for_letter",
        staticmethod(lambda letter: seen.append(letter) or _D0),
    )
    monkeypatch.setenv("SystemDrive", "3:")
    det = drives.DriveDetector()
    assert det._system_disk_paths() == {_D0}
    assert seen == ["C"]


def test_other_system_disks_include_pagefile_and_boot_volumes(monkeypatch):
    """B15: disks hosting the pagefile or a boot/system volume join the
    exclusion, and the extra lookups are cached for the process."""
    monkeypatch.setattr(drives, "_OTHER_SYSTEM_CACHE", None)

    def _map(letter: str) -> str | None:
        return {
            "C": r"\\.\PHYSICALDRIVE0",
            "E": r"\\.\PHYSICALDRIVE2",
        }.get(letter.upper())

    monkeypatch.setattr(
        drives.DriveDetector, "_physical_drive_for_letter", staticmethod(_map)
    )

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Reg:
        HKEY_LOCAL_MACHINE = 0

        @staticmethod
        def OpenKey(_root, _path):
            return _Key()

        @staticmethod
        def QueryValueEx(_key, _name):
            return (["E:\\pagefile.sys 0 0"], 7)

    monkeypatch.setattr(drives, "winreg", _Reg)

    wmi_calls = {"n": 0}

    class _FakeWMI:
        @staticmethod
        def Win32_Volume():
            wmi_calls["n"] += 1
            return [
                types.SimpleNamespace(
                    DriveLetter="E:", BootVolume=True, SystemVolume=False
                ),
                types.SimpleNamespace(
                    DriveLetter="C:", BootVolume=False, SystemVolume=True
                ),
                types.SimpleNamespace(
                    DriveLetter="F:", BootVolume=False, SystemVolume=False
                ),
            ]

    monkeypatch.setattr(drives.wmi, "WMI", lambda: _FakeWMI)

    det = drives.DriveDetector()
    assert det._other_system_disk_paths() == {
        r"\\.\PHYSICALDRIVE0",
        r"\\.\PHYSICALDRIVE2",
    }
    # Second call served from the process cache — no new WMI query.
    det._other_system_disk_paths()
    assert wmi_calls["n"] == 1

    # The extras join the main exclusion (OS disk C: -> PHYSICALDRIVE0).
    _set_system_drive(monkeypatch, "C:")
    assert det._system_disk_paths() == {
        r"\\.\PHYSICALDRIVE0",
        r"\\.\PHYSICALDRIVE2",
    }


# ------------------------------------------------------- WMI detector path ----


def test_wmi_detector_emits_exact_size_bytes(monkeypatch):
    """Fleet flash regression: the WMI path must publish the raw byte count
    so capacity gates and manifests never depend on GB-rounded values."""
    disk = types.SimpleNamespace(
        DeviceID=r"\\.\PHYSICALDRIVE1",
        MediaType="Removable Media",
        InterfaceType="USB",
        Size="8004636160",
        Caption="Fake Stick",
        Model="Fake Stick",
        SerialNumber="SN-FAKE-1",
    )
    conn = types.SimpleNamespace(Win32_DiskDrive=lambda: [disk])
    monkeypatch.setattr(drives.wmi, "WMI", lambda: conn)

    det = drives.DriveDetector()
    monkeypatch.setattr(det, "_system_disk_paths", lambda: set())
    monkeypatch.setattr(det, "_drive_letters", lambda d: ["E"])

    result = det._list_with_wmi()

    assert len(result) == 1
    assert result[0]["size_bytes"] == 8_004_636_160
    assert result[0]["size_gb"] == 8
    assert result[0]["physical_path"] == r"\\.\PHYSICALDRIVE1"
    assert result[0]["serial"] == "SN-FAKE-1"
