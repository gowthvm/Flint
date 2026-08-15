"""UI feature tests: expert default, toggle animation geometry,
verify-page scroll, bottom-bar page scoping and dots-menu ticks."""

import pytest
from PyQt6.QtWidgets import QScrollArea


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


def test_expert_panel_visible_by_default(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert s.get("expert_mode") is True
        assert not w._expert_options_body.isHidden()
        assert w._partition_combo.isEnabled()
    finally:
        w._shutdown()


def test_expert_mode_can_still_be_turned_off(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        w._expert_toggle.setChecked(False)
        assert w._expert_options_body.isHidden()
        assert not w._expert_toggle.isHidden()
        assert not w._partition_combo.isEnabled()
        assert s.get("expert_mode") is False
        w._expert_toggle.setChecked(True)
        assert not w._expert_options_body.isHidden()
        assert w._partition_combo.isEnabled()
        assert s.get("expert_mode") is True
    finally:
        w._shutdown()


def test_toggle_knob_moves_left_when_off(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"verify_after_write": True})
    try:
        toggle = w._verify_toggle
        assert toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(True)
        toggle.setChecked(False, animate=False)
        assert not toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(False)
        toggle.setChecked(True, animate=False)
        assert toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(True)
    finally:
        w._shutdown()


def test_toggle_starts_left_when_off(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"verify_after_write": False})
    try:
        toggle = w._verify_toggle
        assert not toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(False)
    finally:
        w._shutdown()


def test_verify_page_is_scrollable(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        assert isinstance(w._pages.widget(2), QScrollArea)
        assert w._pages.widget(2).widgetResizable()
    finally:
        w._shutdown()


def test_bottombar_hidden_on_history_and_verify_pages(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        assert not w._bottombar.isHidden()
        w._on_nav_clicked(1)
        assert w._pages.currentIndex() == 2
        assert w._bottombar.isHidden()
        w._on_nav_clicked(2)
        assert w._pages.currentIndex() == 1
        assert w._bottombar.isHidden()
        w._on_nav_clicked(0)
        assert w._pages.currentIndex() == 0
        assert not w._bottombar.isHidden()
    finally:
        w._shutdown()


def test_bottombar_stays_visible_when_nav_blocked_while_busy(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        w._writing = True
        w._on_nav_clicked(1)
        assert w._pages.currentIndex() == 0
        assert not w._bottombar.isHidden()
        w._writing = False
    finally:
        w._shutdown()


def test_dots_menu_opens_settings_page(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        texts = [a.text() for a in w._build_dots_menu().actions()]
        assert texts == ["Settings", "", "Check for updates\u2026"]
        w._build_dots_menu().actions()[0].trigger()
        assert w._pages.currentIndex() == 3
        assert w._bottombar.isHidden()
    finally:
        w._shutdown()


def test_flash_without_drive_opens_picker_when_drives_exist(
    qapp, tmp_path, monkeypatch
):
    w = _make_window(qapp, tmp_path, seed={"expert_mode": False})
    try:
        w._drives = [{"physical_path": r"\\.\PHYSICALDRIVE1"}]
        opened = []
        monkeypatch.setattr(w, "_show_drive_picker", lambda: opened.append(1))
        w._iso_zone._path = "C:\\fake.iso"
        w._on_flash_clicked()
        assert opened
    finally:
        w._shutdown()


def test_app_icon_loaded_from_flint_ico(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        icon = w._make_flint_icon()
        assert not icon.isNull()
    finally:
        w._shutdown()


def test_flash_without_drive_errors_when_none_detected(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"expert_mode": False})
    try:
        w._drives = []
        w._iso_zone._path = "C:\\fake.iso"
        w._on_flash_clicked()
        assert "plug one in first" in w._progress._error.text()
    finally:
        w._shutdown()


def _screen_area():
    from PyQt6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def test_restored_geometry_clamped_inside_screen(qapp, tmp_path):
    """A window whose saved position hangs off the right edge must be
    pulled back so nothing is cut off at the border."""
    available = _screen_area()
    if available is None:
        pytest.skip("no screen in this environment")

    w = _make_window(qapp, tmp_path)
    try:
        w.resize(
            min(900, available.width() - 20),
            min(580, available.height() - 20),
        )
        w.move(available.right() - w.width() // 2 + 150, available.top() + 40)
        w._clamp_to_screen()
        frame = w.frameGeometry()
        assert frame.left() >= available.left()
        assert frame.right() <= available.right()
        assert frame.top() >= available.top()
        assert frame.bottom() <= available.bottom()
    finally:
        w._shutdown()


def test_fully_offscreen_geometry_centered_on_screen(qapp, tmp_path):
    """A saved position outside every screen must fall back to a centered
    placement instead of opening unreachable."""
    available = _screen_area()
    if available is None:
        pytest.skip("no screen in this environment")

    w = _make_window(qapp, tmp_path)
    try:
        w.resize(
            min(900, available.width() - 20),
            min(580, available.height() - 20),
        )
        w.move(available.right() + 500, available.top() + 100)
        w._clamp_to_screen()
        frame = w.frameGeometry()
        assert frame.intersects(available)
        assert abs(frame.center().x() - available.center().x()) <= 3
        assert abs(frame.center().y() - available.center().y()) <= 3
    finally:
        w._shutdown()
