"""B04: archives never reach a raw write - flash gates, queue filters
and sidecar evaluation against the extracted payload."""

import hashlib
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    import core.settings as s

    original = s.SETTINGS_PATH
    s.SETTINGS_PATH = tmp_path / "s.json"
    s._CACHE = None
    yield
    s._CACHE = None
    s.SETTINGS_PATH = original


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    from ui import dialogs

    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(dialogs, "completion", lambda *a, **k: None)
    monkeypatch.setattr(dialogs, "inform", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _open_system_disk_guard(monkeypatch):
    from core.drives import DriveDetector

    monkeypatch.setattr(
        DriveDetector, "_system_disk_paths", lambda self: set()
    )


def _make_window(qapp, tmp_path):
    import core.settings as s

    s.set_many(onboarding_seen=True, theme="dark")
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def _drive():
    return {
        "serial": "SER123",
        "physical_path": "\\\\.\\PHYSICALDRIVE9",
        "size_gb": 1.0,
        "letter": "Z:",
        "letters": ["Z:"],
        "model": "Stick SER123",
        "name": "Stick SER123",
    }


def _archive(tmp_path) -> str:
    path = tmp_path / "os.zip"
    path.write_bytes(b"PK\x03\x04" + os.urandom(64))
    return str(path)


def _flash_gates(qapp, tmp_path, monkeypatch, state: str) -> str:
    """Run _on_flash_clicked under a forced zone state; return the strip."""
    from ui import dialogs

    confirms: list[dict] = []

    def _recorder(_parent, **kwargs):
        confirms.append(kwargs)
        return True

    monkeypatch.setattr(dialogs, "confirm", _recorder)
    w = _make_window(qapp, tmp_path)
    try:
        w._iso_state = lambda: state  # type: ignore[method-assign]
        w._on_flash_clicked()
        assert confirms == [], "confirmation must not be reached"
        assert not w._writing
        assert w._writer is None
        return w._progress.message
    finally:
        w._shutdown()


def test_flash_refuses_while_archive_decompresses(
    qapp, tmp_path, monkeypatch
):
    from ui.window import _MSG_DECOMPRESSING

    assert _flash_gates(qapp, tmp_path, monkeypatch, "decompressing") == (
        _MSG_DECOMPRESSING
    )


def test_flash_refuses_failed_archive(qapp, tmp_path, monkeypatch):
    from ui.window import _MSG_ARCHIVE_FAILED

    assert _flash_gates(qapp, tmp_path, monkeypatch, "failed") == (
        _MSG_ARCHIVE_FAILED
    )


def test_flash_refuses_missing_image(qapp, tmp_path, monkeypatch):
    from ui.window import _MSG_NO_IMAGE

    assert _flash_gates(qapp, tmp_path, monkeypatch, "empty") == (
        _MSG_NO_IMAGE
    )


def test_queue_item_refuses_compressed_archive(
    qapp, tmp_path, monkeypatch
):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    archive = _archive(tmp_path)
    fails: list[str] = []
    begun: list[str] = []
    monkeypatch.setattr(
        window_mod.MainWindow,
        "_fail_queue",
        lambda self, reason: fails.append(reason),
    )
    monkeypatch.setattr(
        window_mod.MainWindow,
        "_begin_write",
        lambda self, iso, *a, **k: begun.append(iso),
    )
    try:
        w._current_drive = _drive()
        w._queue_items = [archive]
        w._queue_list.addItem(archive)
        w._start_queue_item(0)
        assert begun == []
        assert fails and "compressed archive" in fails[0]
    finally:
        w._shutdown()


def test_queue_add_dialog_skips_archives(qapp, tmp_path, monkeypatch):
    from ui import window as window_mod

    class _FileDialog:
        @staticmethod
        def getOpenFileNames(*_args, **_kwargs):
            return ([_archive(tmp_path), str(tmp_path / "plain.iso")], "")

    (tmp_path / "plain.iso").write_bytes(b"x" * 32)
    monkeypatch.setattr(window_mod, "QFileDialog", _FileDialog)
    w = _make_window(qapp, tmp_path)
    try:
        w._on_queue_add_clicked()
        assert w._queue_images() == [str(tmp_path / "plain.iso")]
        assert "can't be queued" in w._progress.message
    finally:
        w._shutdown()


def test_archive_selection_skips_sidecar_evaluation(
    qapp, tmp_path, monkeypatch
):
    """The sidecar belongs to the payload, never to the archive."""
    w = _make_window(qapp, tmp_path)
    archive = _archive(tmp_path)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload bytes")
    digest = hashlib.sha256(b"payload bytes").hexdigest()
    (tmp_path / "payload.sha256").write_text(digest + "  payload.bin\n")
    zone = w._iso_zone
    try:
        zone._path = archive
        zone._compressed_source = True
        zone._decompressed_path = None
        w._on_iso_selected(archive)
        assert w._sidecar_status == "missing"
        # Control: the very same file is evaluated when not compressed.
        zone._compressed_source = False
        zone._path = str(payload)
        w._on_iso_selected(str(payload))
        assert w._sidecar_status == "pending"
    finally:
        w._shutdown()


def test_sidecar_checked_against_extracted_payload(
    qapp, tmp_path, monkeypatch
):
    """After hashing, the sidecar next to the archive validates the
    extracted bytes (sidecar_dir wiring in _on_iso_hash_ready)."""
    w = _make_window(qapp, tmp_path)
    archive = _archive(tmp_path)
    payload = tmp_path / "payload.bin"
    data = b"payload bytes"
    payload.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / "payload.sha256").write_text(digest + "  payload.bin\n")
    zone = w._iso_zone
    try:
        zone._path = archive
        zone._compressed_source = True
        zone._decompressed_path = str(payload)
        w._on_iso_hash_ready(str(payload), True, digest)
        assert w._sidecar_status == "ok"
        assert w._iso_sha256_cache[str(payload)] == digest
        # U09: the archive (the user's selection) is what gets remembered.
        assert w._recent_paths() == [archive]
        # A payload with no matching digest must not report ok.
        w._on_iso_hash_ready(str(payload), True, "b" * 64)
        assert w._sidecar_status == "mismatch"
    finally:
        w._shutdown()
