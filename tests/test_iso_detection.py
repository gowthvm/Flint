"""Bootloader / hybrid ISO detection tests (Prompt 4).

Covers the fast hybrid heuristic (ISO9660 marker, MBR boot signature,
partition table, isohybrid marker, El Torito boot record), the writer-side
``is_iso_hybrid`` wrapper, write-mode resolution and the UI behaviour for
hybrid images (file-copy/partition options disabled + tooltip). No real
drives are touched.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from iso_fixture import build_iso, linux_casper_tree

from core import diskpart, iso, writer
from core.iso import is_hybrid_iso

_EL_TORITO = 17 * 2048


def _plain_iso(tmp_path, name="plain.iso"):
    path = tmp_path / name
    build_iso(str(path), linux_casper_tree())
    return str(path)


def _patch_head(path, *, mbr_sig=True, partition=False, isohybrid=False,
                el_torito=False):
    head = bytearray(512)
    if partition:
        head[446] = 0x00
        head[447] = 0x20
    if isohybrid:
        head[432:441] = b"ISOHYBRID"
    if mbr_sig:
        head[510:512] = b"\x55\xaa"
    with open(path, "r+b") as f:
        f.write(head)
    if el_torito:
        with open(path, "r+b") as f:
            f.seek(_EL_TORITO)
            f.write(
                b"\x00" + b"CD001" + b"\x01" + b"EL TORITO SPECIFICATION"
                + b"\x00" * 2040
            )
    return str(path)


def _hybrid_iso(tmp_path, name="hybrid.iso", **kw):
    return _patch_head(_plain_iso(tmp_path, name), **kw)


# ---------------------------------------------------------------------------
# detection heuristics
# ---------------------------------------------------------------------------

def test_plain_iso_not_hybrid(tmp_path):
    assert is_hybrid_iso(_plain_iso(tmp_path)) is False


def test_el_torito_alone_not_hybrid(tmp_path):
    path = _plain_iso(tmp_path, "el-torito.iso")
    _patch_head(path, el_torito=True)
    assert iso.has_el_torito(path) is True
    assert is_hybrid_iso(path) is False


def test_hybrid_with_partition_table(tmp_path):
    assert is_hybrid_iso(_hybrid_iso(tmp_path, partition=True)) is True


def test_hybrid_with_isohybrid_marker_only(tmp_path):
    assert is_hybrid_iso(_hybrid_iso(tmp_path, isohybrid=True)) is True


def test_mbr_without_partition_or_marker_not_hybrid(tmp_path):
    assert is_hybrid_iso(_hybrid_iso(tmp_path)) is False


def test_missing_mbr_signature_not_hybrid(tmp_path):
    path = _hybrid_iso(tmp_path, mbr_sig=False, partition=True)
    assert is_hybrid_iso(path) is False


def test_mbr_without_iso9660_not_hybrid(tmp_path):
    path = tmp_path / "mbr.bin"
    blob = bytearray(2048 * 18)
    blob[510:512] = b"\x55\xaa"
    blob[446] = 0x00
    blob[447] = 0x20
    path.write_bytes(bytes(blob))
    assert is_hybrid_iso(str(path)) is False


def test_missing_file_and_short_file(tmp_path):
    assert is_hybrid_iso(str(tmp_path / "nope.iso")) is False
    small = tmp_path / "small.iso"
    small.write_bytes(b"\x00" * 100)
    assert is_hybrid_iso(str(small)) is False


def test_has_el_torito_false_on_plain(tmp_path):
    assert iso.has_el_torito(_plain_iso(tmp_path)) is False


# ---------------------------------------------------------------------------
# writer wrapper + write-mode resolution
# ---------------------------------------------------------------------------

def test_writer_is_iso_hybrid(tmp_path):
    assert writer.is_iso_hybrid(_hybrid_iso(tmp_path, partition=True)) is True
    assert writer.is_iso_hybrid(_plain_iso(tmp_path)) is False


def test_resolve_write_mode_hybrid_forces_dd(tmp_path):
    hybrid = _hybrid_iso(tmp_path, partition=True)
    assert diskpart.resolve_write_mode("filecopy", hybrid) == "dd"
    assert diskpart.resolve_write_mode("dd", hybrid) == "dd"
    assert diskpart.resolve_write_mode("auto", hybrid) == "dd"


def test_resolve_write_mode_plain_filecopy_kept(tmp_path):
    plain = _plain_iso(tmp_path)
    assert diskpart.resolve_write_mode("filecopy", plain) == "filecopy"


# ---------------------------------------------------------------------------
# UI behaviour
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    import core.settings as s

    original = s.SETTINGS_PATH
    s.SETTINGS_PATH = tmp_path / "s.json"
    s._CACHE = None
    yield
    s._CACHE = None
    s.SETTINGS_PATH = original


def _make_window(qapp, tmp_path):
    import core.settings as s

    s.set_many(onboarding_seen=True, expert_mode=True, theme="dark")
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


def test_hybrid_iso_disables_filecopy_options(qapp, tmp_path):
    hybrid = _hybrid_iso(tmp_path, partition=True)
    w = _make_window(qapp, tmp_path)
    try:
        w._iso_zone.load_iso(hybrid)
        _wait_iso(w, qapp)
        assert w._iso_hybrid is True
        assert not w._mode_combo.isEnabled()
        assert w._mode_combo.currentData() == "dd"
        assert not w._filesystem_combo.isEnabled()
        assert not w._partition_combo.isEnabled()
        assert not w._target_combo.isEnabled()
        assert "raw write recommended" in w._mode_combo.toolTip().lower()
    finally:
        w._shutdown()


def test_plain_iso_keeps_filecopy_options_enabled(qapp, tmp_path):
    plain = _plain_iso(tmp_path)
    w = _make_window(qapp, tmp_path)
    try:
        w._iso_zone.load_iso(plain)
        _wait_iso(w, qapp)
        assert w._iso_hybrid is False
        assert w._mode_combo.isEnabled()
        assert w._filesystem_combo.isEnabled()
        assert w._partition_combo.isEnabled()
        assert w._target_combo.isEnabled()
        assert w._mode_combo.toolTip() == ""
    finally:
        w._shutdown()


def test_clear_iso_resets_hybrid_state(qapp, tmp_path):
    hybrid = _hybrid_iso(tmp_path, partition=True)
    w = _make_window(qapp, tmp_path)
    try:
        w._iso_zone.load_iso(hybrid)
        _wait_iso(w, qapp)
        assert w._iso_hybrid is True
        w._iso_zone.clear_iso()
        _wait_iso(w, qapp)
        assert w._iso_hybrid is False
        assert w._mode_combo.isEnabled()
    finally:
        w._shutdown()
