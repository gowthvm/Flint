"""U15: background durable-write executor (FIFO worker + bounded flush)."""

import time

import pytest

from core import writeback


@pytest.fixture(autouse=True)
def _normalized():
    writeback.disable()
    yield
    writeback.disable()


def test_submit_runs_inline_while_disabled():
    ran: list[int] = []
    writeback.submit(lambda: ran.append(1))
    assert ran == [1]
    assert writeback.enabled() is False


def test_enabled_submit_is_fifo_and_flush_waits():
    order: list[str] = []
    writeback.enable()
    assert writeback.enabled()
    writeback.submit(lambda: (time.sleep(0.05), order.append("a")))
    writeback.submit(lambda: order.append("b"))
    assert writeback.flush(5.0) is True
    assert order == ["a", "b"]


def test_failing_job_does_not_kill_worker():
    order: list[str] = []

    def boom() -> None:
        raise RuntimeError("nope")

    writeback.enable()
    writeback.submit(boom)
    writeback.submit(lambda: order.append("after"))
    assert writeback.flush(5.0) is True
    assert order == ["after"]


def test_disable_flushes_pending_saves():
    landed: list[int] = []
    writeback.enable()
    writeback.submit(lambda: landed.append(1))
    writeback.disable()
    assert landed == [1]
    assert writeback.enabled() is False


def test_flush_is_immediate_when_idle():
    assert writeback.flush(0.1) is True
