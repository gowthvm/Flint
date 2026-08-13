"""Persistence detection + creation tests (Prompt 3).

Covers Linux-ISO detection heuristics (casper / filesystem.squashfs / live),
the persistence UI visibility rule, persistence module behaviour and the
writer's persistence dispatch. No real drives are touched.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from iso_fixture import build_iso, linux_casper_tree, linux_live_tree

from core import diskpart, iso, persistence

# ---------------------------------------------------------------------------
# detection heuristics
# ---------------------------------------------------------------------------

def test_detect_linux_casper_iso(tmp_path):
    path = tmp_path / "ubuntu.iso"
    build_iso(str(path), linux_casper_tree())
    assert iso.detect_linux_iso(str(path)) is True
    assert iso.detect_windows_iso(str(path)) is False


def test_detect_linux_live_iso(tmp_path):
    path = tmp_path / "debian-live.iso"
    build_iso(str(path), linux_live_tree())
    assert iso.detect_linux_iso(str(path)) is True


def test_list_iso_paths(tmp_path):
    path = tmp_path / "ubuntu.iso"
    build_iso(str(path), linux_casper_tree())
    paths = iso.list_iso_paths(str(path))
    assert "casper" in paths
    assert "casper/filesystem.squashfs" in paths
    assert "casper/vmlinuz" in paths
    assert "boot/grub/grub.cfg" in paths


def test_detect_linux_on_plain_image(tmp_path):
    path = tmp_path / "plain.bin"
    path.write_bytes(b"\x00" * 4096)
    assert iso.detect_linux_iso(str(path)) is False
    assert iso.detect_windows_iso(str(path)) is False


def test_detect_missing_file(tmp_path):
    missing = str(tmp_path / "nope.iso")
    assert iso.detect_linux_iso(missing) is False
    assert iso.detect_windows_iso(missing) is False


# ---------------------------------------------------------------------------
# persistence module
# ---------------------------------------------------------------------------

def test_persistence_style_casper():
    assert persistence.persistence_style({"casper/vmlinuz"}) == "casper"
    assert persistence.persistence_style({"live/vmlinuz"}) == "live"


def test_patch_grub_adds_persistent_keyword():
    text = (
        "menuentry 'Try Ubuntu' {\n"
        "    linux /casper/vmlinuz quiet splash\n"
        "    initrd /casper/initrd\n"
        "}\n"
    )
    patched = persistence._patch_grub(text, "persistent")
    assert "persistent" in patched
    # no double-adding
    assert patched.count("persistent") == 1


def test_patch_syslinux_adds_persistent_keyword():
    text = (
        "default live\n"
        "label live\n"
        "  kernel /live/vmlinuz\n"
        "  append initrd=/live/initrd quiet\n"
    )
    patched = persistence._patch_syslinux(text, "persistence")
    assert "quiet persistence" in patched


def test_create_persistence_live_overlay(tmp_path):
    root = str(tmp_path) + "\\"
    ok, _ = persistence.create_persistence(
        root, 512, {"live/filesystem.squashfs"}
    )
    assert ok is True
    conf = tmp_path / "live" / "persistence.conf"
    assert conf.is_file()
    assert conf.read_text() == "/ union\n"


def test_create_persistence_casper_without_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "_mke2fs_candidates", list)
    root = str(tmp_path) + "\\"
    ok, msg = persistence.create_persistence(
        root, 128, {"casper/filesystem.squashfs"}
    )
    assert ok is False
    assert "NOT formatted" in msg
    assert (tmp_path / "casper-rw").is_file()


def test_create_persistence_casper_with_mke2fs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        persistence, "_mke2fs_candidates", lambda: [["mke2fs.exe"]]
    )

    def fake_run(args, capture_output=True, text=True, check=False):
        calls.append(list(args))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(persistence.subprocess, "run", fake_run)
    root = str(tmp_path) + "\\"
    ok, msg = persistence.create_persistence(
        root, 256, {"casper/vmlinuz"}
    )
    assert ok is True
    assert "formatted" in msg
    assert calls and calls[0][0].endswith("mke2fs.exe")
    assert calls[0][1] == "-t" and calls[0][2] == "ext4"


def test_create_persistence_falls_back_after_wsl_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        persistence,
        "_mke2fs_candidates",
        lambda: [["wsl.exe", "mke2fs"], ["mke2fs.exe"]],
    )

    def fake_run(args, capture_output=True, text=True, check=False):
        argv = list(args)
        calls.append(argv)
        if argv[0].endswith("wsl.exe"):
            return type(
                "R", (), {"returncode": 1, "stdout": "", "stderr": "no distro"}
            )()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(persistence.subprocess, "run", fake_run)
    root = str(tmp_path) + "\\"
    ok, _ = persistence.create_persistence(
        root, 256, {"casper/vmlinuz"}
    )
    assert ok is True
    assert calls[0][0].endswith("wsl.exe")
    assert calls[1][0].endswith("mke2fs.exe")


def test_create_persistence_patches_grub_on_drive(tmp_path, monkeypatch):
    grub = tmp_path / "boot" / "grub"
    grub.mkdir(parents=True)
    cfg = grub / "grub.cfg"
    cfg.write_text("linux /casper/vmlinuz quiet\n")
    monkeypatch.setattr(persistence, "_mke2fs_candidates", list)
    root = str(tmp_path) + "\\"
    ok, _ = persistence.create_persistence(
        root, 64, {"casper/filesystem.squashfs"}
    )
    assert "persistent" in cfg.read_text()
    assert (tmp_path / "casper-rw").is_file()
    assert ok is False  # formatting unavailable, but config was patched


# ---------------------------------------------------------------------------
# writer dispatch (no real drives touched)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def _wait_iso(w, qapp, timeout=15000):
    if w._iso_zone._worker is not None:
        w._iso_zone._worker.wait(timeout)
    if w._iso_zone._analyzer is not None:
        w._iso_zone._analyzer.wait(timeout)
    for _ in range(20):
        qapp.processEvents()


def test_writer_persistence_dispatches(qapp, monkeypatch, tmp_path):
    from core.writer import UsbWriter

    seen = {}
    monkeypatch.setattr(diskpart, "prepare_partition", lambda n, s, f: "E")
    monkeypatch.setattr(diskpart, "copy_iso_files", lambda a, b: None)
    monkeypatch.setattr(
        persistence,
        "create_persistence",
        lambda root, mb, paths: seen.update(root=root, mb=mb, paths=paths)
        or (True, "persistence created"),
    )

    iso_path = tmp_path / "ubuntu.iso"
    build_iso(str(iso_path), linux_casper_tree())
    writer = UsbWriter(
        str(iso_path),
        r"\\.\PHYSICALDRIVE3",
        write_mode="filecopy",
        persistence=True,
        persistence_size_mb=2048,
    )
    notes = []
    finished = []
    writer.note.connect(notes.append)
    writer.finished.connect(lambda ok, msg: finished.append((ok, msg)))
    writer.run()

    assert finished == [(True, "")]
    assert seen["root"] == "E:\\"
    assert seen["mb"] == 2048
    assert "casper" in seen["paths"]
    assert notes == ["persistence created"]


def test_writer_persistence_skipped_for_raw_mode(qapp, monkeypatch, tmp_path):
    from core.writer import UsbWriter

    iso_path = tmp_path / "ubuntu.iso"
    build_iso(str(iso_path), linux_casper_tree())
    writer = UsbWriter(
        str(iso_path),
        r"\\.\PHYSICALDRIVE3",
        write_mode="dd",
        persistence=True,
    )
    modes = []
    reached = []
    writer.mode.connect(modes.append)
    # Stub the raw-write body so the test never opens a real drive.
    monkeypatch.setattr(writer, "_run_inner", lambda: reached.append(1))
    writer.run()
    assert modes == ["dd"]
    assert reached == [1]


# ---------------------------------------------------------------------------
# UI visibility
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


def test_persistence_option_shown_for_casper_iso(qapp, tmp_path):
    iso_path = tmp_path / "ubuntu.iso"
    build_iso(str(iso_path), linux_casper_tree())
    w = _make_window(qapp, tmp_path)
    try:
        w._set_expert_mode(True)
        w._iso_zone.load_iso(str(iso_path))
        _wait_iso(w, qapp)
        assert w._iso_linux is True
        assert not w._persistence_toggle.isHidden()
        assert not w._persistence_size.isHidden()
        assert not w._persistence_unit.isHidden()
    finally:
        w._shutdown()


def test_persistence_option_hidden_for_non_linux(qapp, tmp_path):
    plain = tmp_path / "plain.iso"
    plain.write_bytes(b"\x00" * 8192)
    w = _make_window(qapp, tmp_path)
    try:
        w._set_expert_mode(True)
        w._iso_zone.load_iso(str(plain))
        _wait_iso(w, qapp)
        assert w._iso_linux is False
        assert w._persistence_toggle.isHidden()
    finally:
        w._shutdown()


def test_persistence_option_hidden_without_expert_mode(qapp, tmp_path):
    iso_path = tmp_path / "ubuntu.iso"
    build_iso(str(iso_path), linux_casper_tree())
    w = _make_window(qapp, tmp_path)
    try:
        w._iso_zone.load_iso(str(iso_path))
        _wait_iso(w, qapp)
        assert w._iso_linux is True
        assert w._persistence_toggle.isHidden()
    finally:
        w._shutdown()
