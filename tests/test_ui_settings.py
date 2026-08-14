"""Settings page: navigation, theme radios, expert sync, tray-on-close
toggle and close-event behavior."""

import pytest
from PyQt6.QtWidgets import QApplication


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


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def setToolTip(self, text) -> None:
        pass

    def showMessage(self, *args) -> None:
        self.messages.append(args)


def test_close_to_tray_default_off(qapp, tmp_path):
    import core.settings as s

    assert s.get("close_to_tray") is False


def test_settings_page_reachable_from_nav(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        assert len(w._nav_items) == 4
        w._on_nav_clicked(3)
        assert w._pages.currentIndex() == 3
        assert w._bottombar.isHidden()
        w._on_nav_clicked(0)
        assert w._pages.currentIndex() == 0
        assert not w._bottombar.isHidden()
    finally:
        w._shutdown()


def test_dots_menu_has_only_settings(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        texts = [a.text() for a in w._build_dots_menu().actions()]
        assert texts == ["Settings"]
    finally:
        w._shutdown()


def test_close_tray_toggle_persists(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert not w._close_to_tray_toggle.isChecked()
        w._close_to_tray_toggle.setChecked(True)
        assert s.get("close_to_tray") is True
        w._close_to_tray_toggle.setChecked(False)
        assert s.get("close_to_tray") is False
    finally:
        w._shutdown()


def test_settings_expert_toggle_syncs_with_write_page(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        w._settings_expert_toggle.setChecked(False)
        assert not w._expert_toggle.isChecked()
        assert w._expert_options_body.isHidden()
        w._expert_toggle.setChecked(True)
        assert w._settings_expert_toggle.isChecked()
        assert not w._expert_options_body.isHidden()
    finally:
        w._shutdown()


def test_theme_radio_applies_theme(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert w._theme_radios["dark"].isChecked()
        w._theme_radios["light"].setChecked(True)
        assert s.get("theme") == "light"
        assert w._theme_radios["light"].isChecked()
        w._theme_radios["dark"].setChecked(True)
        assert s.get("theme") == "dark"
    finally:
        w._shutdown()


def test_close_blocked_while_busy(qapp, tmp_path, monkeypatch):
    from ui import dialogs as d

    warned = []
    monkeypatch.setattr(
        d, "inform", lambda parent, **kw: warned.append(kw)
    )
    w = _make_window(qapp, tmp_path)
    w.show()
    try:
        w._writing = True
        w.close()
        assert w.isVisible()
        assert not w.isHidden()
        assert warned, "busy close must explain itself without a tray"
        w._writing = False
    finally:
        w._shutdown()


def test_close_hides_to_tray_when_enabled(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"close_to_tray": True})
    w._tray = _FakeTray()
    w.show()
    try:
        w.close()
        assert w.isHidden()
    finally:
        w._shutdown()


def test_close_quits_when_disabled(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    calls = []

    class _FakeApp:
        def quit(self):
            calls.append(1)

        def processEvents(self):
            pass

    monkeypatch.setattr(QApplication, "instance", lambda: _FakeApp())
    w.show()
    try:
        w.close()
        assert calls
    finally:
        w._shutdown()
