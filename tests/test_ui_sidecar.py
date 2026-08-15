"""GUI sidecar wiring: the drop zone must re-emit hash completion so the
window can evaluate the sidecar against the real digest. Regression for
the dead ``hash_done`` connection that left sidecar checks pinned to
"pending" forever."""

import hashlib
import os

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
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


def _make_window(qapp, tmp_path, seed=None):
    import core.settings as s

    values = {"onboarding_seen": True, "theme": "dark"}
    values.update(seed or {})
    s.set_many(**values)
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def _write_image(tmp_path, data: bytes) -> str:
    path = tmp_path / "test.iso"
    path.write_bytes(data)
    return str(path)


def _load_and_wait(w, path: str, qapp) -> None:
    w._iso_zone.load_iso(path)
    assert w._iso_zone._worker is not None
    w._iso_zone._worker.wait(10000)
    qapp.processEvents()


def test_sidecar_matches_reports_ok(qapp, tmp_path):
    data = os.urandom(256 * 1024 + 3)
    image = _write_image(tmp_path, data)
    digest = hashlib.sha256(data).hexdigest()
    sidecar = tmp_path / "test.iso.sha256"
    sidecar.write_text(f"{digest}  test.iso\n")

    w = _make_window(qapp, tmp_path)
    try:
        _load_and_wait(w, image, qapp)
        assert w._iso_zone.digest == digest
        assert w._sidecar_status == "ok"
        assert "matches" in w._sidecar_label.text()
        assert not w._sidecar_label.isHidden()
    finally:
        w._shutdown()


def test_sidecar_mismatch_blocks_flash(qapp, tmp_path):
    data = os.urandom(256 * 1024 + 3)
    image = _write_image(tmp_path, data)
    sidecar = tmp_path / "test.iso.sha256"
    sidecar.write_text(f"{'b' * 64}  test.iso\n")

    w = _make_window(qapp, tmp_path)
    try:
        _load_and_wait(w, image, qapp)
        assert w._sidecar_status == "mismatch"
        assert "MISMATCH" in w._sidecar_label.text()
        assert "blocked" in w._sidecar_label.text().lower()
        assert hasattr(w, "_iso_zone")
    finally:
        w._shutdown()


def test_sidecar_pending_only_before_hash_finishes(qapp, tmp_path):
    data = os.urandom(256 * 1024 + 3)
    image = _write_image(tmp_path, data)
    digest = hashlib.sha256(data).hexdigest()
    sidecar = tmp_path / "test.iso.sha256"
    sidecar.write_text(f"{digest}  test.iso\n")

    w = _make_window(qapp, tmp_path)
    try:
        w._iso_zone.load_iso(image)
        # Selection happens before hash completion: status is pending.
        assert w._sidecar_status == "pending"
        assert w._iso_zone._worker is not None
        w._iso_zone._worker.wait(10000)
        qapp.processEvents()
        # Hash completion re-evaluates the sidecar.
        assert w._sidecar_status == "ok"
    finally:
        w._shutdown()