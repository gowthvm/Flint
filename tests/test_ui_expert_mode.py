"""Expert-mode visibility and inline help (?) buttons for the write page."""

import pytest


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

    s.set_many(onboarding_seen=True, expert_mode=False, theme="dark")
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def test_expert_panel_hidden_and_controls_disabled_by_default(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        assert w._expert_options_body.isHidden()
        assert not w._expert_toggle.isHidden()
        assert not w._partition_combo.isEnabled()
        assert not w._target_combo.isEnabled()
        assert not w._filesystem_combo.isEnabled()
        assert not w._mode_combo.isEnabled()
    finally:
        w._shutdown()


def test_enabling_expert_mode_shows_panel_and_enables_controls(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        w._expert_toggle.setChecked(True)
        assert not w._expert_options_body.isHidden()
        assert w._partition_combo.isEnabled()
        assert w._target_combo.isEnabled()
        assert w._filesystem_combo.isEnabled()
        assert w._mode_combo.isEnabled()
        assert s.get("expert_mode") is True
    finally:
        w._shutdown()


def test_disabling_expert_mode_hides_panel_and_persists(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        w._expert_toggle.setChecked(True)
        w._expert_toggle.setChecked(False)
        assert w._expert_options_body.isHidden()
        assert not w._expert_toggle.isHidden()
        assert not w._partition_combo.isEnabled()
        assert s.get("expert_mode") is False
    finally:
        w._shutdown()


def test_expert_choices_persist_to_settings(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        w._expert_toggle.setChecked(True)
        w._partition_combo.setCurrentIndex(w._partition_combo.findData("gpt"))
        w._target_combo.setCurrentIndex(w._target_combo.findData("uefi"))
        w._filesystem_combo.setCurrentIndex(
            w._filesystem_combo.findData("ntfs")
        )
        w._mode_combo.setCurrentIndex(w._mode_combo.findData("filecopy"))
        w._buffer_combo.setCurrentIndex(w._buffer_combo.findData(16))
        assert s.get("partition_scheme") == "gpt"
        assert s.get("target_system") == "uefi"
        assert s.get("filesystem") == "ntfs"
        assert s.get("write_mode") == "filecopy"
        assert s.get("chunk_size_mb") == 16
    finally:
        w._shutdown()


def test_help_buttons_have_specific_tooltips(qapp, tmp_path):
    from PyQt6.QtWidgets import QPushButton

    w = _make_window(qapp, tmp_path)
    try:
        buttons = [
            b
            for b in w._expert_options_body.findChildren(QPushButton)
            if b.objectName() == "helpBtn"
        ]
        buttons += [
            b
            for b in w._verify_options_card.findChildren(QPushButton)
            if b.objectName() == "helpBtn"
        ]
        assert len(buttons) == 11
        tips = [b.tip_text() for b in buttons]
        assert all(tips), "every help button must carry a tip"
        assert len(set(tips)) == 10, "exactly one tip is shared (buffer size)"
        joined = " ".join(tips)
        for keyword in (
            "GPT",
            "UEFI",
            "FAT32",
            "Raw (DD)",
            "CreateFile",
            "casper-rw",
            "dism",
            "SHA-256",
            "4 KiB",
            "MiB",
            "TPM",
        ):
            assert keyword in joined, f"missing keyword: {keyword}"
    finally:
        w._shutdown()


def test_help_button_hover_shows_instant_fading_bubble(qapp, tmp_path):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QEnterEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton

    from ui.window import TipBubble

    w = _make_window(qapp, tmp_path)
    try:
        btn = next(
            b
            for b in w._expert_options_body.findChildren(QPushButton)
            if b.objectName() == "helpBtn"
        )
        bubble = w._help_bubble
        assert isinstance(bubble, TipBubble)
        assert not bubble.isVisible()

        enter = QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1))
        btn.enterEvent(enter)
        QTest.qWait(10)
        assert bubble.isVisible(), "bubble must appear on first hover"
        assert bubble._label.text() == btn.tip_text()
        QTest.qWait(100)
        assert bubble.windowOpacity() == 1.0, "bubble must reach full opacity"

        btn.leaveEvent(QEvent(QEvent.Type.Leave))
        QTest.qWait(100)
        assert not bubble.isVisible(), "bubble must fade out and hide"
    finally:
        w._shutdown()
