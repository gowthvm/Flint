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