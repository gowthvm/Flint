"""Auto-update check worker lifecycle (L37).

The quiet 7-day check must retire its ``UpdateCheckWorker`` the same way
the manual check does. The ``finished_check`` signal is emitted as the
last statement of ``run()``, so a handler that simply does
``self._update_checker = None`` drops the last reference to a QThread
whose ``run()`` is still on the stack - the exact bug
``_on_update_check_done`` documents as T4.

No real network request is made: ``UpdateCheckWorker`` is replaced with a
signal-only fake that is never started as a real thread.
"""

import os
from typing import ClassVar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QObject, pyqtSignal


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _FakeCheckWorker(QObject):
    finished_check = pyqtSignal(bool, str, object)
    instances: ClassVar[list["_FakeCheckWorker"]] = []

    def __init__(self) -> None:
        super().__init__()
        self.started = False
        self.instances.append(self)

    def start(self) -> None:
        self.started = True


@pytest.fixture(autouse=True)
def _seed_settings(_isolated_settings_store):
    from core import settings

    settings.set_many(onboarding_seen=True, theme="dark")


@pytest.fixture(autouse=True)
def _reset_fake_workers():
    _FakeCheckWorker.instances = []
    yield
    _FakeCheckWorker.instances = []


def test_auto_update_check_done_retires_its_worker(qapp, _make_window):
    window = _make_window()
    worker = _FakeCheckWorker()
    window._update_checker = worker

    window._on_auto_update_check_done(False, "boom", None, worker)

    assert window._update_checker is None
    assert worker in _FakeCheckWorker.instances
    assert worker in window._retired_workers, (
        "the handler must keep the QThread alive until shutdown"
    )


def test_auto_update_check_done_without_a_worker_still_clears_it(
    qapp, _make_window
):
    window = _make_window()
    window._update_checker = _FakeCheckWorker()

    window._on_auto_update_check_done(True, "", None)

    assert window._update_checker is None


def test_maybe_auto_check_hands_the_worker_to_its_handler(
    qapp, _make_window, monkeypatch
):
    import ui.window as win

    monkeypatch.setattr(win, "UpdateCheckWorker", _FakeCheckWorker)

    window = _make_window()
    assert window._update_checker is None

    window._maybe_auto_check_updates()

    assert len(_FakeCheckWorker.instances) == 1
    worker = _FakeCheckWorker.instances[0]
    assert worker.started is True
    assert window._update_checker is worker

    worker.finished_check.emit(False, "offline", None)

    assert window._update_checker is None
    assert worker in window._retired_workers, (
        "the connection must pass the worker through, not just the payload"
    )
