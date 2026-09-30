"""Flash queue UI: list management, queue state machine and guards
(destructive flows are faked so no drive or modal dialog is touched)."""

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    from ui import dialogs as d

    monkeypatch.setattr(d, "completion", lambda *a, **k: None)
    monkeypatch.setattr(d, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(d, "inform", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _open_system_disk_guard(monkeypatch):
    """B02: state-machine tests never probe the real system disk."""
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


FAKE = {
    "physical_path": r"\\.\PHYSICALDRIVE7",
    "serial": "S9D4F2",
    "model": "USB Stick 64GB",
    "size_gb": 64,
    "letter": "E",
    "letters": ["E"],
    "bus_type": "USB",
    "name": "USB Stick 64GB",
}


def test_queue_add_remove_clear(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        a, b = str(tmp_path / "a.iso"), str(tmp_path / "b.iso")
        w._queue_list.addItem(a)
        w._queue_list.addItem(b)
        assert w._queue_images() == [a, b]
        w._queue_list.setCurrentRow(0)
        w._on_queue_remove_clicked()
        assert w._queue_images() == [b]
        w._on_queue_clear_clicked()
        assert w._queue_images() == []
    finally:
        w._shutdown()


def test_flash_queue_requires_drive(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        w._queue_list.addItem(str(tmp_path / "a.iso"))
        w._on_flash_queue_clicked()
        assert not w._queue_active
    finally:
        w._shutdown()


def test_flash_queue_requires_images(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        w._current_drive = FAKE
        w._on_flash_queue_clicked()
        assert not w._queue_active
    finally:
        w._shutdown()


def test_queue_advances_and_completes(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    w._current_drive = FAKE
    w._begin_write = lambda *a, **k: None  # type: ignore[method-assign]
    try:
        a, b = str(tmp_path / "a.iso"), str(tmp_path / "b.iso")
        w._queue_items = [a, b]
        w._queue_list.addItem(a)
        w._queue_list.addItem(b)
        w._queue_active = True
        w._queue_index = 0
        w._queue_last_succeeded = True
        w._maybe_start_next_queue_item()
        assert w._queue_index == 1
        assert w._queue_list.item(0).text().startswith("done")
        assert w._queue_list.item(1).text().startswith("flashing")
        assert w._queue_active
        w._queue_last_succeeded = True
        w._maybe_start_next_queue_item()
        assert not w._queue_active
        assert w._queue_list.item(1).text().startswith("done")
    finally:
        w._shutdown()


def test_queue_stops_on_failure(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    w._current_drive = FAKE
    try:
        a, b = str(tmp_path / "a.iso"), str(tmp_path / "b.iso")
        w._queue_items = [a, b]
        w._queue_list.addItem(a)
        w._queue_list.addItem(b)
        for i in range(w._queue_list.count()):
            w._mark_queue_item(i, "pending")
        w._queue_active = True
        w._queue_index = 0
        w._queue_last_succeeded = False
        w._maybe_start_next_queue_item()
        assert not w._queue_active
        assert w._queue_index == 1
        assert w._queue_list.item(0).text().startswith("failed")
        assert w._queue_list.item(1).text().startswith("pending")
    finally:
        w._shutdown()


def test_queue_busy_blocks_other_actions(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    w._current_drive = FAKE
    try:
        assert not w._busy()
        w._queue_active = True
        assert w._busy()
    finally:
        w._shutdown()


def test_fleet_stop_button_survives_control_disable(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        w._set_controls_enabled(False)
        assert not w._flash_btn.isEnabled()
        assert not w._queue_add_btn.isEnabled()
        assert w._fleet_stop_btn.isEnabled()
        w._set_controls_enabled(True)
        assert w._fleet_stop_btn.isEnabled()
    finally:
        w._shutdown()


def test_refuse_if_system_disk_guard(qapp, tmp_path, monkeypatch):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    drive = dict(FAKE)
    try:
        monkeypatch.setattr(
            window_mod.DriveDetector, "_system_disk_paths", lambda self: None
        )
        assert w._refuse_if_system_disk(drive) is True
        assert "Could not verify system disk status" in w._progress._error.text()

        monkeypatch.setattr(
            window_mod.DriveDetector,
            "_system_disk_paths",
            lambda self: {FAKE["physical_path"]},
        )
        assert w._refuse_if_system_disk(drive) is True
        assert w._progress._error.text() == "Refusing to write to the system disk"

        monkeypatch.setattr(
            window_mod.DriveDetector,
            "_system_disk_paths",
            lambda self: {r"\\.\PHYSICALDRIVE0"},
        )
        assert w._refuse_if_system_disk(drive) is False
        assert w._refuse_if_system_disk(None) is False
    finally:
        w._shutdown()


def test_flash_start_refused_by_system_disk_guard(qapp, tmp_path, monkeypatch):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    prompts: list[dict] = []
    try:
        w._current_drive = dict(FAKE)
        monkeypatch.setattr(
            window_mod.DriveDetector,
            "_system_disk_paths",
            lambda self: {FAKE["physical_path"]},
        )
        monkeypatch.setattr(
            "ui.window.dialogs.confirm",
            lambda parent, **kw: prompts.append(kw) or True,
        )
        w._on_flash_clicked()
        assert not prompts, "a refused start must never reach the confirm"
        assert not w._writing
        assert "system disk" in w._progress._error.text().lower()
    finally:
        w._shutdown()


def test_drives_ready_keeps_list_on_transient_error(qapp, tmp_path):
    from types import SimpleNamespace

    w = _make_window(qapp, tmp_path)
    drive = dict(FAKE)
    try:
        w._drives = [drive]
        w._current_drive = drive
        w._detector = SimpleNamespace(last_error="scan failed")
        w._on_drives_ready([])
        assert w._drives == [drive]
        assert w._current_drive == drive
    finally:
        w._shutdown()


def test_drives_ready_clears_on_empty_without_error(qapp, tmp_path):
    from types import SimpleNamespace

    w = _make_window(qapp, tmp_path)
    drive = dict(FAKE)
    other = dict(FAKE, physical_path=r"\\.\PHYSICALDRIVE8", serial="OTHER")
    try:
        w._drives = [drive]
        w._current_drive = drive
        w._detector = SimpleNamespace(last_error=None)
        w._on_drives_ready([])
        assert w._drives == []
        assert w._current_drive is None

        w._drives = [drive]
        w._current_drive = drive
        w._on_drives_ready([other])
        assert w._drives == [other]
        assert w._current_drive is None
    finally:
        w._shutdown()


def test_fleet_tick_passes_skip_flashed_setting(qapp, tmp_path, monkeypatch):
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    try:
        w._queue_list.addItem(str(tmp_path / "a.iso"))
        monkeypatch.setattr(
            "ui.window.dialogs.input_text", lambda *a, **k: ("ARM", True)
        )
        captured: list[bool] = []

        def _fake_pick(drives, session, now=None, skip_flashed=False):
            captured.append(bool(skip_flashed))

        monkeypatch.setattr(window_mod.fleet, "pick_candidate", _fake_pick)
        w._fleet_toggle.setChecked(True)
        assert captured and captured[-1] is False
        w._fleet_skip_flashed.setChecked(True)
        w._fleet_tick()
        assert captured[-1] is True
    finally:
        w._shutdown()

def test_queue_persists_across_restart(qapp, tmp_path):
    """U09(c): the queue survives a restart; missing files are dropped."""
    w = _make_window(qapp, tmp_path)
    try:
        keep = tmp_path / "keep.iso"
        keep.write_bytes(b"x")
        gone = str(tmp_path / "gone.iso")  # never created on disk
        w._queue_list.addItem(str(keep))
        w._queue_list.addItem(gone)
        w._persist_queue()

        from core import paths as core_paths

        queue_file = core_paths.APP_DIR / "queue.json"
        assert queue_file.is_file()

        w._queue_list.clear()
        w._restore_queue()
        assert w._queue_images() == [str(keep)]
    finally:
        w._shutdown()
