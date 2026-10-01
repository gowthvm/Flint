"""Themed dialog tests: FlintDialog structure, run results, and the
completion popups fired at the end of flash / verify / wipe."""

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

    s.set_many(onboarding_seen=True, theme="dark")
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def test_dialog_structure_and_kind_icon(qapp):
    from ui import dialogs

    dlg = dialogs.FlintDialog(
        None,
        kind="success",
        title="Flash complete",
        message="The image was written and verified.",
        buttons=[("Copy report", "ghost", "copy"), ("Close", "primary", "close")],
    )
    assert dlg.objectName() == "flintDialog"
    texts = [w.text() for w in dlg.children() if hasattr(w, "text")]
    assert "Flash complete" in texts
    assert "The image was written and verified." in texts
    assert any(t == "\u2713\ufe0e" for t in texts)
    dlg.deleteLater()


def test_dialog_accept_sets_result(qapp):
    from ui import dialogs

    dlg = dialogs.FlintDialog(
        None,
        kind="info",
        title="T",
        message="M",
        buttons=[("Close", "primary", "close")],
    )
    dlg._accept("close")
    assert dlg._result == "close"
    assert dlg.result() == 1
    dlg.deleteLater()


def test_dialog_reject_keeps_no_result(qapp):
    from ui import dialogs

    dlg = dialogs.FlintDialog(
        None, kind="info", title="T", message="M", buttons=[]
    )
    dlg.reject()
    assert dlg._result is None
    dlg.deleteLater()


def test_confirm_result_mapping(qapp, monkeypatch):
    from ui import dialogs

    monkeypatch.setattr(dialogs.FlintDialog, "run", lambda self: "yes")
    assert dialogs.confirm(None, kind="warning", title="T", message="M",
                           accept="Go") is True
    monkeypatch.setattr(dialogs.FlintDialog, "run", lambda self: "no")
    assert dialogs.confirm(None, kind="warning", title="T", message="M",
                           accept="Go") is False


def test_inform_checkbox(qapp, monkeypatch):
    from ui import dialogs

    monkeypatch.setattr(dialogs.FlintDialog, "run", lambda self: "close")
    dlg = dialogs.inform(
        None, kind="info", title="T", message="M", check="Don't show again"
    )
    assert dlg._check is not None
    assert dlg.checked() is False
    dlg._check.setChecked(True)
    assert dlg.checked() is True
    dlg.deleteLater()


def test_input_text(qapp, monkeypatch):
    from ui import dialogs

    captured = {}

    def fake_run(self):
        captured["dlg"] = self
        self._field.setText("ABCD")
        return "yes"

    monkeypatch.setattr(dialogs.FlintDialog, "run", fake_run)
    text, ok = dialogs.input_text(None, title="T", message="Type it")
    assert ok and text == "ABCD"
    monkeypatch.setattr(dialogs.FlintDialog, "run", lambda self: "no")
    text2, ok2 = dialogs.input_text(None, title="T", message="Type it")
    assert not ok2 and text2 == ""


def test_completion_default_buttons(qapp, monkeypatch):
    from ui import dialogs

    captured = {}

    def fake_run(self):
        captured["dlg"] = self
        return "close"

    monkeypatch.setattr(dialogs.FlintDialog, "run", fake_run)
    result = dialogs.completion(
        None, kind="success", title="T", message="M"
    )
    assert result == "close"
    from PyQt6.QtWidgets import QPushButton

    labels = [
        (b.text(), b.objectName())
        for b in captured["dlg"].findChildren(QPushButton)
    ]
    assert labels == [
        ("Eject drive", "ghost"),
        ("Copy report", "ghost"),
        ("Close", "primary"),
    ]


def _capture(monkeypatch):
    calls = []

    def fake_completion(parent, *, kind, title, message, buttons=None):
        calls.append({"kind": kind, "title": title, "message": message})

    import ui.dialogs as dialogs_mod

    monkeypatch.setattr(dialogs_mod, "completion", fake_completion)
    return calls


def test_flash_success_popup(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        calls = _capture(monkeypatch)
        w._finish_flash(True, "", "e" * 64)
        assert len(calls) == 1
        assert calls[0]["kind"] == "success"
        assert calls[0]["title"] == "Flash complete"
        assert "verified" in calls[0]["message"]
    finally:
        w._shutdown()


def test_flash_cancel_popup(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        calls = _capture(monkeypatch)
        w._finish_flash(False, "cancelled", None)
        assert len(calls) == 1
        assert calls[0]["kind"] == "warning"
        assert calls[0]["title"] == "Write cancelled"
    finally:
        w._shutdown()


def test_flash_fail_popup(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        calls = _capture(monkeypatch)
        w._finish_flash(False, "write failed: 87", None)
        assert len(calls) == 1
        assert calls[0]["kind"] == "error"
        assert calls[0]["title"] == "Flash failed"
    finally:
        w._shutdown()


def test_flash_fail_popup_copy_report_is_not_a_dead_click(
    qapp, tmp_path, monkeypatch
):
    """The failure dialog offers Copy report; its result used to be
    discarded, so the button did nothing."""
    w = _make_window(qapp, tmp_path)
    try:
        copied: list[bool] = []
        monkeypatch.setattr(
            w, "_on_copy_report_clicked", lambda: copied.append(True)
        )

        import ui.dialogs as dialogs_mod

        offered: list = []

        def fake_completion(parent, *, kind, title, message, buttons=None):
            offered.extend(buttons or [])
            return "copy"

        monkeypatch.setattr(dialogs_mod, "completion", fake_completion)

        w._finish_flash(False, "write failed: 87", None)

        assert ("Copy report", "ghost", "copy") in offered
        assert copied == [True]
    finally:
        w._shutdown()


def test_page_verify_popups(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        calls = _capture(monkeypatch)
        w._on_page_verify_finished(True, "All good")
        assert calls[-1]["kind"] == "success"
        assert calls[-1]["title"] == "Verification passed"
        assert calls[-1]["message"] == "All good"
        w._on_page_verify_finished(False, "hash mismatch")
        assert calls[-1]["kind"] == "error"
        assert calls[-1]["title"] == "Verification failed"
    finally:
        w._shutdown()


def test_wipe_popup(qapp, tmp_path, monkeypatch):
    import core.history as h

    w = _make_window(qapp, tmp_path)
    try:
        calls = _capture(monkeypatch)
        w._current_drive = {
            "model": "TestDrive", "name": "TestDrive", "letter": "K:",
            "serial": "S9", "physical_path": r"\\.\PHYSICALDRIVE9",
        }
        w._active_write_drive = w._current_drive
        w._write_duration = 1.0
        w._on_wipe_finished(True, "")
        assert calls[-1]["kind"] == "success"
        assert calls[-1]["title"] == "Drive wiped"
        w._on_wipe_finished(False, "cancelled")
        assert calls[-1]["kind"] == "warning"
        assert calls[-1]["title"] == "Wipe cancelled"
        w._on_wipe_finished(False, "disk error")
        assert calls[-1]["kind"] == "error"
        assert calls[-1]["title"] == "Wipe failed"
        assert h.load_history()
    finally:
        w._shutdown()


def test_wipe_history_records_wipe_operation(qapp, tmp_path, monkeypatch):
    import ui.window as window_mod

    w = _make_window(qapp, tmp_path)
    captured: dict = {}

    def fake_flash_report(
        iso_name, drive_model, duration_seconds, verified, success, **kwargs
    ):
        captured.update(kwargs)
        captured["iso"] = iso_name
        return {"iso": iso_name, "operation": kwargs.get("operation", "flash")}

    monkeypatch.setattr(window_mod, "flash_report", fake_flash_report)
    try:
        _capture(monkeypatch)
        w._current_drive = {
            "model": "TestDrive", "name": "TestDrive", "letter": "K:",
            "serial": "S9", "physical_path": r"\\.\PHYSICALDRIVE9",
        }
        w._active_write_drive = w._current_drive
        w._write_duration = 1.0
        w._on_wipe_finished(True, "")
        assert captured["operation"] == "wipe"
        assert captured["drive_path"] == r"\\.\PHYSICALDRIVE9"
        assert captured["iso"] == "\u2014 wipe \u2014"
    finally:
        w._shutdown()


def test_cancel_write_asks_for_confirmation(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    confirms: list[dict] = []
    cancels: list[str] = []

    class _Worker:
        def cancel(self) -> None:
            cancels.append("write")

    try:
        w._writer = _Worker()  # type: ignore[assignment]
        monkeypatch.setattr(
            "ui.window.dialogs.confirm",
            lambda parent, **kw: confirms.append(kw) or False,
        )
        assert w._on_cancel_clicked() is False
        assert confirms[0]["title"] == "Cancel write?"
        assert confirms[0]["accept"] == "Cancel write"
        assert confirms[0]["accept_style"] == "danger"
        assert not cancels, "declining must leave the write running"

        monkeypatch.setattr(
            "ui.window.dialogs.confirm",
            lambda parent, **kw: confirms.append(kw) or True,
        )
        assert w._on_cancel_clicked() is True
        assert cancels

        confirms.clear()
        w._writer = None
        assert w._on_cancel_clicked() is True
        assert not confirms, "an idle cancel must not ask for confirmation"
    finally:
        w._writer = None
        w._shutdown()


def test_backup_and_clone_success_popups(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    try:
        calls = _capture(monkeypatch)
        w._backup_out = str(tmp_path / "backup.img")
        w._backup_drive = {
            "model": "SrcDrive", "name": "SrcDrive", "serial": "S1",
            "physical_path": r"\\.\PHYSICALDRIVE4", "size_gb": 8,
        }
        w._on_drive_operation_finished(True, "")
        assert calls[-1]["kind"] == "success"
        assert calls[-1]["title"] == "Backup complete"
        assert "SrcDrive" in calls[-1]["message"]
        assert "backup.img" in calls[-1]["message"]

        w._clone_source = {
            "model": "SrcDrive", "name": "SrcDrive", "serial": "S1",
            "physical_path": r"\\.\PHYSICALDRIVE4", "size_gb": 8,
        }
        w._clone_target = {
            "model": "DstDrive", "name": "DstDrive", "serial": "S2",
            "physical_path": r"\\.\PHYSICALDRIVE5", "size_gb": 16,
        }
        w._on_drive_operation_finished(True, "")
        assert calls[-1]["kind"] == "success"
        assert calls[-1]["title"] == "Clone complete"
        assert "DstDrive" in calls[-1]["message"]
        assert "16 GB" in calls[-1]["message"]
    finally:
        w._shutdown()
