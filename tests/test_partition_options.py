"""Unit tests for expert partition/boot options (Prompt 2).

Covers settings persistence defaults, partition-scheme resolution, diskpart
script generation, format commands, hybrid-ISO detection and the writer's
file-copy dispatch. All subprocess/disk helpers are mocked - no real drives
are touched.
"""

import os

import pytest

from core import diskpart
from core.iso import is_hybrid_iso


class _FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# settings defaults
# ---------------------------------------------------------------------------

def test_settings_defaults(tmp_path):
    import core.settings as s

    s.SETTINGS_PATH = tmp_path / "s.json"
    assert s.get("expert_mode") is False
    assert s.get("partition_scheme") == "auto"
    assert s.get("target_system") == "auto"
    assert s.get("filesystem") == "fat32"
    assert s.get("write_mode") == "auto"
    s.set_many(
        expert_mode=True,
        partition_scheme="gpt",
        target_system="uefi",
        filesystem="ntfs",
        write_mode="filecopy",
    )
    assert s.get("expert_mode") is True
    assert s.get("partition_scheme") == "gpt"
    assert s.get("target_system") == "uefi"
    assert s.get("filesystem") == "ntfs"
    assert s.get("write_mode") == "filecopy"


# ---------------------------------------------------------------------------
# partition scheme resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "scheme,target,expected",
    [
        ("auto", "auto", "gpt"),
        ("auto", "uefi", "gpt"),
        ("auto", "legacy", "mbr"),
        ("gpt", "legacy", "gpt"),
        ("mbr", "uefi", "mbr"),
        ("GPT", "auto", "gpt"),
        ("", "", "gpt"),
    ],
)
def test_resolve_partition_scheme(scheme, target, expected):
    assert diskpart.resolve_partition_scheme(scheme, target) == expected


# ---------------------------------------------------------------------------
# diskpart script generation
# ---------------------------------------------------------------------------

def test_build_diskpart_script_gpt():
    script = diskpart.build_diskpart_script(3, "gpt")
    lines = script.strip().splitlines()
    assert "select disk 3" in lines
    assert "clean" in lines
    assert "convert gpt" in lines
    assert "create partition primary" in lines
    assert "assign" in lines


def test_build_diskpart_script_mbr():
    script = diskpart.build_diskpart_script(2, "mbr")
    assert "select disk 2" in script
    assert "convert mbr" in script
    assert "convert gpt" not in script


def test_build_diskpart_script_auto_defaults_to_gpt():
    script = diskpart.build_diskpart_script(1, "auto")
    assert "convert gpt" in script


# ---------------------------------------------------------------------------
# drive number extraction
# ---------------------------------------------------------------------------

def test_drive_number_from_path():
    assert diskpart.drive_number_from_path(r"\\.\PHYSICALDRIVE3") == 3
    assert diskpart.drive_number_from_path(r"\\.\PHYSICALDRIVE12") == 12


def test_drive_number_from_path_rejects_non_physical():
    with pytest.raises(ValueError):
        diskpart.drive_number_from_path(r"C:\some\file.bin")


# ---------------------------------------------------------------------------
# write mode resolution + hybrid detection
# ---------------------------------------------------------------------------

def _write_iso(tmp_path, name="hybrid.iso", *, marker=True, partitions=True,
               sig=True):
    path = tmp_path / name
    blob = bytearray(2048 * 17)
    if marker:
        blob[32769:32774] = b"CD001"
    if sig:
        blob[510:512] = b"\x55\xaa"
    if partitions:
        blob[446] = 0x00  # partition entry: type 0 with nonzero start
        blob[447] = 0x20
    path.write_bytes(bytes(blob))
    return str(path)


def test_is_hybrid_iso_detects_hybrid(tmp_path):
    assert is_hybrid_iso(_write_iso(tmp_path)) is True


def test_is_hybrid_iso_missing_partition_table(tmp_path):
    assert (
        is_hybrid_iso(_write_iso(tmp_path, partitions=False)) is False
    )


def test_is_hybrid_iso_missing_mbr_signature(tmp_path):
    assert is_hybrid_iso(_write_iso(tmp_path, sig=False)) is False


def test_is_hybrid_iso_not_an_iso(tmp_path):
    assert is_hybrid_iso(_write_iso(tmp_path, marker=False)) is False


def test_is_hybrid_iso_missing_file(tmp_path):
    assert is_hybrid_iso(str(tmp_path / "nope.iso")) is False


def test_resolve_write_mode_defaults_to_dd(tmp_path):
    plain = _write_iso(tmp_path, "plain.iso", partitions=False)
    assert diskpart.resolve_write_mode("auto", plain) == "dd"
    assert diskpart.resolve_write_mode("dd", plain) == "dd"


def test_resolve_write_mode_filecopy(tmp_path):
    plain = _write_iso(tmp_path, "plain.iso", partitions=False)
    assert diskpart.resolve_write_mode("filecopy", plain) == "filecopy"


def test_resolve_write_mode_hybrid_forced_to_dd(tmp_path):
    hybrid = _write_iso(tmp_path)
    assert diskpart.resolve_write_mode("filecopy", hybrid) == "dd"


# ---------------------------------------------------------------------------
# prepare_partition command sequence (diskpart -> letter -> format)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_subprocess(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        argv = list(args[0])
        calls.append(argv)
        if argv[0].endswith("format.com"):
            return _FakeResult()
        if "Get-Partition" in argv[-1]:
            return _FakeResult(stdout="E\n")
        return _FakeResult()

    monkeypatch.setattr(diskpart.subprocess, "run", fake_run)
    return calls


def test_prepare_partition_gpt_ntfs(fake_subprocess):
    letter = diskpart.prepare_partition(3, "gpt", "ntfs")
    assert letter == "E"
    assert len(fake_subprocess) == 3
    # 1) diskpart with a generated script file
    disk_call, ps_call, fmt_call = fake_subprocess
    assert disk_call[0].endswith("diskpart.exe")
    assert disk_call[1] == "/s"
    with open(disk_call[2], encoding="utf-8") as f:
        script = f.read()
    assert "select disk 3" in script and "convert gpt" in script
    # 2) letter resolution via PowerShell
    assert ps_call[0].endswith("powershell.exe")
    assert "Get-Partition -DiskNumber 3" in ps_call[-1]
    # 3) format command with the chosen filesystem
    assert fmt_call[0].endswith("format.com")
    assert fmt_call[1] == "E:"
    assert fmt_call[2] == "/FS:NTFS"
    assert "/Q" in fmt_call and "/Y" in fmt_call


def test_prepare_partition_mbr_fat32(fake_subprocess):
    letter = diskpart.prepare_partition(2, "mbr", "fat32")
    assert letter == "E"
    disk_call = fake_subprocess[0]
    with open(disk_call[2], encoding="utf-8") as f:
        script = f.read()
    assert "convert mbr" in script and "convert gpt" not in script
    fmt_call = fake_subprocess[2]
    assert fmt_call[2] == "/FS:FAT32"


@pytest.mark.parametrize(
    "filesystem,expected_flag",
    [
        ("fat32", "/FS:FAT32"),
        ("ntfs", "/FS:NTFS"),
        ("exfat", "/FS:EXFAT"),
    ],
)
def test_format_command_per_filesystem(fake_subprocess, filesystem, expected_flag):
    diskpart.run_format("F", filesystem)
    fmt_call = fake_subprocess[0]
    assert fmt_call[0].endswith("format.com")
    assert fmt_call[1] == "F:"
    assert expected_flag in fmt_call


def test_prepare_partition_rejects_bad_filesystem(fake_subprocess):
    with pytest.raises(ValueError):
        diskpart.prepare_partition(1, "gpt", "ext4")


def test_prepare_partition_surfaces_diskpart_failure(monkeypatch):
    def fail(*args, **kwargs):
        return _FakeResult(stderr="denied", returncode=5)

    monkeypatch.setattr(diskpart.subprocess, "run", fail)
    with pytest.raises(OSError, match="diskpart"):
        diskpart.prepare_partition(1, "gpt", "fat32")


def test_non_windows_raises_not_implemented(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    with pytest.raises(NotImplementedError, match="Windows"):
        diskpart.prepare_partition(1, "gpt", "fat32")
    with pytest.raises(NotImplementedError, match="Windows"):
        diskpart.run_format("E", "fat32")
    with pytest.raises(NotImplementedError, match="Windows"):
        diskpart.copy_iso_files("x.iso", "E")


# ---------------------------------------------------------------------------
# copy helpers
# ---------------------------------------------------------------------------

def test_copy_iso_files_mounts_copies_dismounts(monkeypatch):
    seen = []

    def fake_run(*args, **kwargs):
        argv = list(args[0])
        seen.append(argv)
        if "Mount-DiskImage" in argv[-1]:
            return _FakeResult(stdout="D\n")
        return _FakeResult()

    monkeypatch.setattr(diskpart.subprocess, "run", fake_run)
    diskpart.copy_iso_files(r"C:\some\image.iso", "E")
    assert any("Mount-DiskImage" in call[-1] for call in seen)
    assert any("Dismount-DiskImage" in call[-1] for call in seen)
    robocopy = next(call for call in seen if call[0] == "robocopy")
    assert robocopy[1] == "D:\\" and robocopy[2] == "E:\\"


# ---------------------------------------------------------------------------
# writer dispatch (no real drives touched)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def test_writer_filecopy_dispatches_to_diskpart(qapp, monkeypatch, tmp_path):
    from core.writer import UsbWriter

    calls = []
    monkeypatch.setattr(
        diskpart, "prepare_partition", lambda n, s, f: calls.append((n, s, f)) or "E"
    )
    monkeypatch.setattr(diskpart, "copy_iso_files", lambda iso, letter: calls.append((iso, letter)))

    plain = _write_iso(tmp_path, "plain.iso", partitions=False)
    writer = UsbWriter(
        plain,
        r"\\.\PHYSICALDRIVE3",
        write_mode="filecopy",
        partition_scheme="gpt",
        filesystem="ntfs",
    )
    modes = []
    phases = []
    finished = []
    writer.mode.connect(modes.append)
    writer.phase.connect(phases.append)
    writer.finished.connect(lambda ok, msg: finished.append((ok, msg)))

    writer.run()

    assert modes == ["filecopy"]
    assert finished == [(True, "")]
    assert ("Preparing partition", "Copying files") == tuple(phases[:2])
    assert calls[0] == (3, "gpt", "ntfs")
    assert calls[1] == (plain, "E")


def test_writer_hybrid_iso_falls_back_to_raw(qapp, monkeypatch, tmp_path):
    from core.writer import UsbWriter

    hybrid = _write_iso(tmp_path)
    writer = UsbWriter(hybrid, r"\\.\PHYSICALDRIVE3", write_mode="filecopy")
    modes = []
    reached = []
    writer.mode.connect(modes.append)
    # Stub the raw-write body so the test never opens a real drive.
    monkeypatch.setattr(writer, "_run_inner", lambda: reached.append(1))
    writer.run()
    assert modes == ["dd"]
    assert reached == [1]
