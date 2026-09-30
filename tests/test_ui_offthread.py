"""U12/U14: per-item hashing and sidecar checks run on workers, never
on the GUI thread, and the fast paths stay synchronous."""

import hashlib
import os
import time
from typing import ClassVar

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
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


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


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


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _FakeWriter:
    """Records construction; never touches a real drive."""

    instances: ClassVar[list] = []

    def __init__(self, iso, drive_path, letters=None, **kwargs):
        self.iso = iso
        self.drive_path = drive_path
        self.letters = letters
        self.kwargs = kwargs
        self.started = False
        for name in (
            "mode",
            "note",
            "progress",
            "speed_mbps",
            "written_bytes",
            "total_bytes",
            "eta_seconds",
            "phase",
            "verify_result",
            "done",
        ):
            setattr(self, name, _Sig())
        _FakeWriter.instances.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def cancel(self):
        pass

    def deleteLater(self):
        pass


def test_fleet_manifest_hash_runs_off_the_gui_thread(
    qapp, tmp_path, monkeypatch
):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    _FakeWriter.instances.clear()
    monkeypatch.setattr(window_mod, "UsbWriter", _FakeWriter)
    monkeypatch.setattr(window_mod, "APP_DIR", tmp_path / "app")
    data = os.urandom(64 * 1024)
    image = tmp_path / "os.img"
    image.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    try:
        w._fleet_busy = True
        w._iso_sha256_cache.pop(str(image), None)
        # U12: the manifest digest used to be computed right here,
        # freezing the GUI for seconds on a large image.
        w._begin_write(
            str(image),
            "\\\\.\\PHYSICALDRIVE9",
            ["Z:"],
            {"write_mode": "auto", "verify_after_write": False},
            _drive(),
        )
        # First pass: hashing queued to a worker, nothing written yet.
        assert _FakeWriter.instances == []
        assert w._hash_worker is not None
        assert w._writing
        assert not w._writer
        assert _wait_until(lambda: w._writer is not None)
        assert len(_FakeWriter.instances) == 1
        assert _FakeWriter.instances[0].started
        assert w._iso_sha256_cache[str(image)] == digest
        manifests = list((tmp_path / "app" / "jobs").glob("*.json"))
        assert len(manifests) == 1
        manifest = window_mod.jobs.load_manifest(manifests[0])
        assert manifest.source_sha256 == digest
        assert manifest.source_size == len(data)
        assert manifest.source_path == str(image)
        assert manifest.state == "writing"
    finally:
        w._shutdown()


def test_queue_item_defers_while_sidecar_check_runs(
    qapp, tmp_path, monkeypatch
):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    begun: list[str] = []
    monkeypatch.setattr(
        window_mod.MainWindow,
        "_begin_write",
        lambda self, iso, *a, **k: begun.append(iso),
    )
    image = tmp_path / "os.img"
    image.write_bytes(os.urandom(4096))
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (tmp_path / "os.img.sha256").write_text(digest + "  os.img\n")
    try:
        w._current_drive = _drive()
        w._queue_items = [str(image)]
        w._queue_list.addItem(str(image))
        # U14: a present sidecar defers the start to a worker.
        w._start_queue_item(0)
        assert begun == []
        assert w._sidecar_worker is not None
        assert _wait_until(lambda: begun != [])
        assert begun == [str(image)]
        assert w._sidecar_worker is None
    finally:
        w._shutdown()


def test_queue_item_without_sidecar_starts_synchronously(
    qapp, tmp_path, monkeypatch
):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    begun: list[str] = []
    monkeypatch.setattr(
        window_mod.MainWindow,
        "_begin_write",
        lambda self, iso, *a, **k: begun.append(iso),
    )
    image = tmp_path / "os.img"
    image.write_bytes(os.urandom(4096))
    try:
        w._current_drive = _drive()
        w._queue_items = [str(image)]
        w._queue_list.addItem(str(image))
        # No sidecar -> the stat-only fast path stays synchronous
        # (tests/test_fleet_ui.py depends on this).
        w._start_queue_item(0)
        assert begun == [str(image)]
        assert w._sidecar_worker is None
    finally:
        w._shutdown()


def test_queue_item_with_unreadable_sidecar_fails_off_thread(
    qapp, tmp_path, monkeypatch
):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    begun: list[str] = []
    fails: list[str] = []
    monkeypatch.setattr(
        window_mod.MainWindow,
        "_begin_write",
        lambda self, iso, *a, **k: begun.append(iso),
    )
    monkeypatch.setattr(
        window_mod.MainWindow,
        "_fail_queue",
        lambda self, reason: fails.append(reason),
    )
    image = tmp_path / "os.img"
    image.write_bytes(os.urandom(4096))
    (tmp_path / "os.img.sha256").write_text("not a digest at all\n")
    try:
        w._current_drive = _drive()
        w._queue_items = [str(image)]
        w._queue_list.addItem(str(image))
        w._start_queue_item(0)
        assert begun == []
        assert _wait_until(lambda: fails != [])
        assert "checksum problem" in fails[0]
        assert begun == []
    finally:
        w._shutdown()
