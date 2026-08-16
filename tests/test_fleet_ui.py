"""Fleet-mode UI tests: arming, drive-handoff, sequencing, failures.

Writes are recorded, never executed against real hardware: `_begin_write`
is patched with a recorder so a fleet flash only exercises the state
machine up to the point a real writer would start.
"""

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


def _make_window(qapp, tmp_path, seed: dict | None = None):
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


def _image(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"x" * 1000)
    return str(path)


def _queue(w, *paths: str) -> None:
    for p in paths:
        w._queue_list.addItem(p)


def _drive(serial: str, size_gb: float = 1.0, **extra) -> dict:
    drive = {
        "serial": serial,
        "physical_path": r"\\.\PHYSICALDRIVE9",
        "size_gb": size_gb,
        "letter": "Z:",
        "letters": ["Z:"],
        "model": f"Stick {serial}",
        "name": f"Stick {serial}",
    }
    drive.update(extra)
    return drive


def _arm(w, monkeypatch, input_text: tuple[str, bool] = ("ARM", True)):
    calls: list[tuple] = []

    def fake_input_text(parent, title="", message="", placeholder=""):
        calls.append((title, message, placeholder))
        return input_text

    monkeypatch.setattr("ui.window.dialogs.input_text", fake_input_text)
    written: list[tuple] = []
    monkeypatch.setattr(
        "ui.window.MainWindow._begin_write",
        lambda self, iso, drive_path, drive_letters, writer_kwargs, drive: (
            written.append(
                (iso, drive_path, tuple(drive_letters), writer_kwargs, drive)
            )
        ),
    )
    w._fleet_toggle.setChecked(True)
    return calls, written


def test_fleet_arm_requires_typed_arm(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        calls, written = _arm(w, monkeypatch, input_text=("no", True))
        assert w._fleet is None
        assert not w._fleet_busy
        assert not w._fleet_toggle.isChecked()
        assert w._fleet_banner.isHidden()
        assert calls and "ARM" in calls[0][2]
        assert not written
    finally:
        w._shutdown()


def test_fleet_arm_cancelled_input(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        _arm(w, monkeypatch, input_text=("", False))
        assert w._fleet is None
        assert not w._fleet_toggle.isChecked()
    finally:
        w._shutdown()


def test_fleet_arm_requires_queue(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        informed: list[tuple] = []
        monkeypatch.setattr("ui.window.dialogs.inform", lambda *a, **k: informed.append(k.get("message", "")))
        _arm(w, monkeypatch)
        assert w._fleet is None
        assert not w._fleet_toggle.isChecked()
        assert informed
        assert "queue" in informed[0].lower()
    finally:
        w._shutdown()


def test_fleet_arm_success_state(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        image = _image(tmp_path, "a.img")
        _queue(w, image)
        _arm(w, monkeypatch)
        assert w._fleet is not None
        assert w._fleet.images == [image]
        assert w._fleet_toggle.isChecked()
        assert not w._fleet_banner.isHidden()
        assert "Armed" in w._fleet_label.text()
    finally:
        w._shutdown()


def test_fleet_arm_blocked_while_busy(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        w._queue_active = True
        calls, written = _arm(w, monkeypatch)
        assert w._fleet is None
        assert not w._fleet_toggle.isChecked()
        assert not calls
        assert not written
    finally:
        w._shutdown()


def test_fleet_tick_starts_flash_on_fitting_drive(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"), _image(tmp_path, "b.img"))
        _, written = _arm(w, monkeypatch)
        assert not written
        drive = _drive("AAA")
        w._drives = [drive]
        w._fleet_tick()
        assert w._fleet_busy
        assert w._fleet_drive == drive
        assert len(written) == 1
    finally:
        w._shutdown()


def test_fleet_drive_plugged_in_triggers_flash(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        _, written = _arm(w, monkeypatch)
        drive = _drive("BBB")
        w._on_drives_ready([drive])
        assert w._fleet_busy
        assert w._fleet_drive == drive
        assert len(written) == 1
        assert written[0][0] == w._fleet.images[0]
    finally:
        w._shutdown()


def test_fleet_sequence_all_images_each_drive(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        images = [_image(tmp_path, "a.img"), _image(tmp_path, "b.img")]
        _queue(w, *images)
        _, written = _arm(w, monkeypatch)
        drive_a = _drive("AAA")
        drive_b = _drive("BBB")
        w._drives = [drive_a, drive_b]
        w._fleet_tick()
        assert w._fleet_drive == drive_a
        assert len(written) == 1
        w._fleet_finish_image(True)
        assert len(written) == 2
        assert written[1][0] == images[1]
        w._fleet_finish_image(True)
        assert w._fleet_drive == drive_b
        assert len(written) == 3
        w._fleet_finish_image(True)
        w._fleet_finish_image(True)
        assert len(written) == 4
        assert w._fleet.done_count == 2
        assert "2 done" in w._fleet_label.text()
        assert not w._fleet_busy
        assert w._fleet_drive is None
        w._fleet_tick()
        assert len(written) == 4
    finally:
        w._shutdown()


def test_fleet_skips_drive_that_does_not_fit(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        _, written = _arm(w, monkeypatch)
        tiny = _drive("TINY", size_gb=0.0000005)
        big = _drive("BIG", size_gb=1.0)
        w._drives = [tiny, big]
        w._fleet_tick()
        assert w._fleet_busy
        assert w._fleet_drive == big
        assert len(written) == 1
    finally:
        w._shutdown()


def test_fleet_failure_disarms(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        _arm(w, monkeypatch)
        w._drives = [_drive("AAA")]
        w._fleet_tick()
        assert w._fleet_busy
        monkeypatch.setattr(
            "ui.window.dialogs.completion", lambda *a, **k: "close"
        )
        w._fleet_finish_image(False)
        assert w._fleet is None
        assert not w._fleet_busy
        assert not w._fleet_toggle.isChecked()
        assert w._fleet_banner.isHidden()
        assert w._fleet_drive is None
    finally:
        w._shutdown()


def test_fleet_stop_while_waiting_disarms(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        _arm(w, monkeypatch)
        w._fleet_stop_btn.click()
        assert w._fleet is None
        assert not w._fleet_toggle.isChecked()
        assert w._fleet_banner.isHidden()
    finally:
        w._shutdown()


def test_fleet_stop_while_writing_cancels(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        _arm(w, monkeypatch)
        w._drives = [_drive("AAA")]
        w._fleet_tick()
        assert w._fleet_busy
        cancelled: list = []
        monkeypatch.setattr("ui.window.MainWindow._on_cancel_clicked", lambda self: cancelled.append(1))
        w._fleet_stop_btn.click()
        assert cancelled
    finally:
        w._shutdown()


def test_fleet_expiry_disarms_with_message(qapp, tmp_path, monkeypatch):
    import core.fleet as fleet_mod

    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        informed: list[tuple] = []
        monkeypatch.setattr("ui.window.dialogs.inform", lambda *a, **k: informed.append(k.get("message", "")))
        _arm(w, monkeypatch)
        assert w._fleet is not None
        monkeypatch.setattr(
            fleet_mod.FleetSession,
            "expired",
            lambda self, now=None: True,
        )
        w._fleet_tick()
        assert w._fleet is None
        assert not w._fleet_toggle.isChecked()
        assert informed
        assert "expired" in informed[0].lower()
    finally:
        w._shutdown()


def test_fleet_toggle_off_disarms(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        _queue(w, _image(tmp_path, "a.img"))
        _arm(w, monkeypatch)
        assert w._fleet is not None
        w._fleet_toggle.setChecked(False)
        assert w._fleet is None
        assert w._fleet_banner.isHidden()
    finally:
        w._shutdown()