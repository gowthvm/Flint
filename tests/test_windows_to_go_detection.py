"""Windows To Go detection + dispatch tests (Prompt 3).

Covers Windows-ISO detection (ISO9660 ``sources/install.wim`` plus the raw
UDF fallback), the Windows To Go UI behaviour (NTFS enforcement, mutual
exclusion with persistence) and the writer's dism-based dispatch. No real
drives are touched.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from iso_fixture import build_iso, linux_casper_tree, windows_tree

from core import diskpart, iso

# ---------------------------------------------------------------------------
# detection heuristics
# ---------------------------------------------------------------------------

def test_detect_windows_iso_tree(tmp_path):
    path = tmp_path / "windows.iso"
    build_iso(str(path), windows_tree())
    assert iso.detect_windows_iso(str(path)) is True
    assert iso.detect_linux_iso(str(path)) is False


def test_detect_windows_iso_esd(tmp_path):
    tree = [
        ("d", "sources", [("f", "install.esd", 2048)]),
    ]
    path = tmp_path / "windows.esd.iso"
    build_iso(str(path), tree)
    assert iso.detect_windows_iso(str(path)) is True


def test_detect_windows_iso_swm(tmp_path):
    tree = [
        ("d", "sources", [("f", "install.swm", 2048)]),
    ]
    path = tmp_path / "windows.swm.iso"
    build_iso(str(path), tree)
    assert iso.detect_windows_iso(str(path)) is True


def test_detect_windows_iso_udf_raw_scan(tmp_path):
    """Pure-UDF style image: no ISO9660 bridge, name stored as UTF-16LE."""
    path = tmp_path / "udf.iso"
    blob = b"\x00" * 1024 + "install.wim".encode("utf-16-le")
    blob += b"\x00" * 4096
    path.write_bytes(blob)
    assert iso.detect_windows_iso(str(path)) is True


def test_detect_windows_iso_ascii_raw_scan(tmp_path):
    """UDF with ASCII file identifiers also falls back to the raw scan."""
    path = tmp_path / "udf2.iso"
    blob = b"\x00" * 2048 + b"sources\\install.wim" + b"\x00" * 4096
    path.write_bytes(blob)
    assert iso.detect_windows_iso(str(path)) is True


def test_detect_windows_not_triggered_by_linux(tmp_path):
    path = tmp_path / "ubuntu.iso"
    build_iso(str(path), linux_casper_tree())
    assert iso.detect_windows_iso(str(path)) is False


def test_list_iso_paths_windows_tree(tmp_path):
    path = tmp_path / "windows.iso"
    build_iso(str(path), windows_tree())
    paths = iso.list_iso_paths(str(path))
    assert "sources/install.wim" in paths
    assert "efi/boot/bootx64.efi" in paths


# ---------------------------------------------------------------------------
# diskpart.apply_windows_image command sequence (mocked)
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_apply_windows_image_runs_dism_and_bcdboot(monkeypatch, tmp_path):
    calls = []

    def fake_run(*args, **kwargs):
        argv = list(args[0])
        calls.append(argv)
        if "Mount-DiskImage" in argv[-1]:
            return _FakeResult(stdout="D\n")
        return _FakeResult()

    monkeypatch.setattr(diskpart.subprocess, "run", fake_run)
    # the mounted ISO letter holds sources/install.wim
    os.makedirs(os.path.join(str(tmp_path), "sources"), exist_ok=True)
    open(os.path.join(str(tmp_path), "sources", "install.wim"), "wb").close()
    monkeypatch.setattr(
        diskpart, "_find_windows_image", lambda letter: f"{letter}:\\sources\\install.wim"
    )

    diskpart.apply_windows_image(r"C:\some\windows.iso", "E")

    dism_call = next(call for call in calls if call[0].endswith("dism.exe"))
    assert dism_call[1] == "/Apply-Image"
    assert dism_call[2] == "/ImageFile:D:\\sources\\install.wim"
    assert dism_call[3] == "/Index:1"
    assert dism_call[4] == "/ApplyDir:E:\\"
    bcd_call = next(call for call in calls if call[0].endswith("bcdboot.exe"))
    assert bcd_call[1] == "E:\\Windows"
    assert "/f" in bcd_call and "ALL" in bcd_call


def test_apply_windows_image_missing_image_fails(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        argv = list(args[0])
        if "Mount-DiskImage" in argv[-1]:
            return _FakeResult(stdout="D\n")
        return _FakeResult()

    monkeypatch.setattr(diskpart.subprocess, "run", fake_run)
    monkeypatch.setattr(diskpart, "_find_windows_image", lambda letter: None)
    with pytest.raises(OSError, match="install"):
        diskpart.apply_windows_image(r"C:\some\windows.iso", "E")


# ---------------------------------------------------------------------------
# writer dispatch (no real drives touched)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def test_writer_wtg_dispatches_to_dism(qapp, monkeypatch, tmp_path):
    from core.writer import UsbWriter

    prepared = []
    applied = []
    copied = []
    monkeypatch.setattr(
        diskpart,
        "prepare_partition",
        lambda n, s, f: prepared.append((n, s, f)) or "E",
    )
    monkeypatch.setattr(
        diskpart,
        "apply_windows_image",
        lambda iso_path, letter: applied.append((iso_path, letter)),
    )
    monkeypatch.setattr(
        diskpart, "copy_iso_files", lambda iso_path, letter: copied.append(letter)
    )

    path = tmp_path / "windows.iso"
    build_iso(str(path), windows_tree())
    writer = UsbWriter(
        str(path),
        r"\\.\PHYSICALDRIVE3",
        write_mode="filecopy",
        windows_to_go=True,
        filesystem="ntfs",
    )
    finished = []
    writer.finished.connect(lambda ok, msg: finished.append((ok, msg)))
    writer.run()

    assert finished == [(True, "")]
    assert prepared == [(3, "auto", "ntfs")]
    assert applied == [(str(path), "E")]
    assert copied == []  # WTG never does a plain file copy


# ---------------------------------------------------------------------------
# UI behaviour
# ---------------------------------------------------------------------------

def _make_window(qapp, tmp_path):
    import core.settings as s

    s.SETTINGS_PATH = tmp_path / "s.json"
    s.set_many(onboarding_seen=True, expert_mode=False, theme="dark")
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def _wait_iso(w, qapp, timeout=15000):
    if w._iso_zone._worker is not None:
        w._iso_zone._worker.wait(timeout)
    if w._iso_zone._analyzer is not None:
        w._iso_zone._analyzer.wait(timeout)
    for _ in range(20):
        qapp.processEvents()


def test_wtg_option_shown_for_windows_iso(qapp, tmp_path):
    path = tmp_path / "windows.iso"
    build_iso(str(path), windows_tree())
    w = _make_window(qapp, tmp_path)
    try:
        w._set_expert_mode(True)
        w._iso_zone.load_iso(str(path))
        _wait_iso(w, qapp)
        assert w._iso_windows is True
        assert not w._wtg_toggle.isHidden()
        assert w._persistence_toggle.isHidden()
    finally:
        w._shutdown()


def test_wtg_forces_ntfs_and_blocks_persistence(qapp, tmp_path):
    path = tmp_path / "windows.iso"
    build_iso(str(path), windows_tree())
    w = _make_window(qapp, tmp_path)
    try:
        w._set_expert_mode(True)
        w._iso_zone.load_iso(str(path))
        _wait_iso(w, qapp)
        w._wtg_toggle.setChecked(True)
        assert w._filesystem_combo.currentData() == "ntfs"
        assert not w._filesystem_combo.isEnabled()
        assert not w._persistence_toggle.isEnabled()
        w._wtg_toggle.setChecked(False)
        assert w._filesystem_combo.isEnabled()
        assert w._persistence_toggle.isEnabled()
    finally:
        w._shutdown()


def test_wtg_hidden_for_linux_iso(qapp, tmp_path):
    path = tmp_path / "ubuntu.iso"
    build_iso(str(path), linux_casper_tree())
    w = _make_window(qapp, tmp_path)
    try:
        w._set_expert_mode(True)
        w._iso_zone.load_iso(str(path))
        _wait_iso(w, qapp)
        assert w._iso_windows is False
        assert w._wtg_toggle.isHidden()
    finally:
        w._shutdown()
