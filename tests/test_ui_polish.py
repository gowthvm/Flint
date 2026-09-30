"""U07/U08/U10/U11/C05/C11/C15: shared refusal text, drop-zone guards,
hotplug announcements and the dead-code cleanups."""

import inspect

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


def test_verify_strip_shares_write_refusal_text(qapp, tmp_path):
    from ui.window import _MSG_BUSY, _MSG_NO_DRIVE, _MSG_NO_IMAGE

    w = _make_window(qapp, tmp_path)
    try:
        w._writing = True
        w._on_page_verify_start()
        assert w._verify_progress.message == _MSG_BUSY
        w._writing = False
        w._current_drive = None
        w._on_page_verify_start()
        assert w._verify_progress.message == _MSG_NO_DRIVE
        # U07: the write strip refuses with the very same constants.
        w._on_flash_clicked()
        assert w._progress.message == _MSG_NO_IMAGE
    finally:
        w._shutdown()


def test_flash_tooltip_uses_shared_refusal_text(qapp, tmp_path):
    from ui.window import _MSG_NO_DRIVE, _MSG_NO_IMAGE

    w = _make_window(qapp, tmp_path)
    image = tmp_path / "boot.iso"
    image.write_bytes(b"mbr")
    try:
        w._drives = [dict(_drive())]
        w._current_drive = None
        w._update_controls_state()
        assert w._flash_btn.toolTip() == _MSG_NO_IMAGE
        w._iso_zone._path = str(image)
        w._current_drive = dict(_drive())
        w._update_controls_state()
        assert w._flash_btn.toolTip() == (
            "Write the image to the selected drive"
        )
        w._current_drive = None
        w._update_controls_state()
        assert w._flash_btn.toolTip() == _MSG_NO_DRIVE
    finally:
        w._shutdown()


def test_verify_zone_browse_guard_blocks_while_busy(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    image = tmp_path / "zone.iso"
    image.write_bytes(b"mbr")
    try:
        w._verify_zone.load_iso(str(image))
        assert w._verify_zone.path == str(image)
        w._verify_zone.clear_iso()
        # U08: the verify drop zone gained the same browse guard the
        # write zone always had - a busy window refuses new selections.
        w._writing = True
        w._verify_zone.load_iso(str(image))
        assert w._verify_zone.path is None
        w._writing = False
        w._verify_zone.load_iso(str(image))
        assert w._verify_zone.path == str(image)
    finally:
        w._writing = False
        w._verify_zone.clear_iso()
        w._shutdown()


class _FakeMenu:
    def exec(self, *_args):
        return None


def test_dots_menu_blocked_while_busy(qapp, tmp_path, monkeypatch):
    from ui.window import _MSG_BUSY, MainWindow

    w = _make_window(qapp, tmp_path)
    opened: list[bool] = []

    def _fake_menu(self):
        opened.append(True)
        return _FakeMenu()

    monkeypatch.setattr(MainWindow, "_build_dots_menu", _fake_menu)
    try:
        # U10: the overflow menu was navigation that stayed live mid-write.
        w._writing = True
        w._show_dots_menu()
        assert opened == []
        assert w._progress.message == _MSG_BUSY
        w._writing = False
        w._show_dots_menu()
        assert opened == [True]
    finally:
        w._shutdown()


def test_first_drive_scan_is_silent_then_hotplug_announces(
    qapp, tmp_path
):
    w = _make_window(qapp, tmp_path)
    notices: list[tuple[str, str]] = []
    w._show_tray_notify = (  # type: ignore[method-assign]
        lambda title, message: notices.append((title, message))
    )
    try:
        # First scan only establishes the baseline.
        w._on_drives_ready([dict(_drive())])
        assert notices == []
        w._on_drives_ready([])
        assert len(notices) == 1
        assert "removed" in notices[0][1]
        w._on_drives_ready([dict(_drive())])
        assert len(notices) == 2
        assert "connected" in notices[1][1]
        # Nothing changed -> no balloon.
        w._on_drives_ready([dict(_drive())])
        assert len(notices) == 2
    finally:
        w._shutdown()


def test_nav_item_dropped_its_badge_plumbing():
    from ui.widgets import NavItem

    params = list(inspect.signature(NavItem.__init__).parameters)
    assert params == ["self", "text", "active"]
    item = NavItem("Write", True)
    assert not hasattr(item, "_badge_label")


def test_write_finish_dropped_the_standalone_verify_call():
    from ui import window as window_mod

    # C05: the write path no longer branches into a second
    # VerifyWorker - in-writer verification covers armed flashes and
    # everything else reports "not verified" honestly. The method
    # itself stays: tests/test_regression.py drives it directly.
    finish_src = inspect.getsource(window_mod.MainWindow._on_write_finished)
    assert "self._start_verify(" not in finish_src
    assert hasattr(window_mod.MainWindow, "_start_verify")
    assert not hasattr(window_mod.MainWindow, "_scroll_to_progress")
    assert not hasattr(window_mod.MainWindow, "_scroll_to_done_bar")
    assert hasattr(window_mod.MainWindow, "_scroll_to_verify_progress")


def test_window_never_reaches_into_widget_privates():
    from ui import window as window_mod

    # C11: the window talks to widget APIs (set_title / message /
    # set_guards / browse), never to their internals.
    src = inspect.getsource(window_mod)
    assert "._title" not in src
    assert "._error" not in src
    assert "._clear_guard" not in src
    assert "._browse_guard" not in src


def test_nav_page_mapping_is_one_shared_table(qapp, tmp_path):
    from ui.window import _NAV_PAGE, _NAV_TITLES

    assert _NAV_PAGE == {0: 0, 1: 2, 2: 1, 3: 3}
    assert _NAV_TITLES[1] == "Verify a drive"
    w = _make_window(qapp, tmp_path)
    try:
        w._on_nav_clicked(1)
        assert w._pages.currentIndex() == 2
        assert w._topbar_title.text() == "Verify a drive"
        w._on_nav_clicked(3)
        assert w._pages.currentIndex() == 3
        assert w._topbar_title.text() == "Settings"
    finally:
        w._shutdown()


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _FakeProbe:
    def __init__(self, path, reported):
        self.path = path
        self.reported = reported
        self.result = _Sig()
        self.started = False

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def cancel(self):
        pass

    def deleteLater(self):
        pass


def test_fake_capacity_probe_is_confirmed_and_read_only(
    qapp, tmp_path, monkeypatch
):
    from ui import dialogs as dialogs_mod
    from ui import window as window_mod

    w = _make_window(qapp, tmp_path)
    confirms: list[dict] = []
    started: list[tuple] = []

    def _recorder(_parent, **kwargs):
        confirms.append(kwargs)
        return True

    monkeypatch.setattr(dialogs_mod, "confirm", _recorder)
    monkeypatch.setattr(
        window_mod,
        "FakeCapacityWorker",
        lambda path, reported: started.append((path, reported))
        or _FakeProbe(path, reported),
    )
    try:
        # Busy: no dialog, no probe.
        w._current_drive = _drive()
        w._writing = True
        w._on_check_fake_clicked()
        assert confirms == []
        assert started == []
        w._writing = False
        # No drive: refuses with the shared text.
        w._current_drive = None
        w._on_check_fake_clicked()
        assert confirms == []
        assert w._progress.message == "Select a target drive first"
        # Confirmed probe reaches the worker with the reported capacity.
        w._current_drive = _drive()
        w._on_check_fake_clicked()
        assert len(confirms) == 1
        assert "read-only" in confirms[0]["message"].lower()
        assert started == [(r"\\.\PHYSICALDRIVE9", 1_000_000_000)]
        assert w._fake_worker is not None
    finally:
        w._shutdown()


def test_fake_capacity_results_surface_as_dialogs(
    qapp, tmp_path, monkeypatch
):
    from ui import dialogs as dialogs_mod

    w = _make_window(qapp, tmp_path)
    shown: list[dict] = []

    def _completion(_parent, **kwargs):
        shown.append(kwargs)
        return "close"

    monkeypatch.setattr(dialogs_mod, "completion", _completion)
    probe = _FakeProbe(r"\\.\PHYSICALDRIVE9", 1_000_000_000)
    w._fake_worker = probe
    try:
        w._on_fake_capacity_result(True, "no data past 1 GB")
        assert shown and shown[0]["kind"] == "error"
        assert w._fake_worker is None
        w._fake_worker = _FakeProbe(r"\\.\PHYSICALDRIVE9", 1_000_000_000)
        w._on_fake_capacity_result(False, "read back normally")
        assert shown[-1]["kind"] == "success"
        assert w._fake_worker is None
    finally:
        w._shutdown()
