"""Background durable-write executor (U15).

``os.fsync`` plus an atomic replace can stall for tens or hundreds of
milliseconds on slow disks or under antivirus scans; running that on the
GUI thread stutters the UI.  Jobs submitted here run FIFO on a single
daemon worker, preserving the read-merge-write ordering (and the
file-lock discipline) of the original synchronous saves.

Disabled by default: the CLI and the test-suite rely on a save landing
before the next statement runs.  The GUI enables it at startup
(``main.main``) and flushes pending jobs on shutdown
(``MainWindow._shutdown``); an ``atexit`` hook backstops the flush.
"""

import atexit
import logging
import queue
import threading
from collections.abc import Callable

logger = logging.getLogger("flint")

_lock = threading.Lock()
_idle = threading.Event()
_idle.set()
_queue: queue.Queue[object] | None = None
_worker: threading.Thread | None = None
_pending = 0
_enabled = False
_atexit_registered = False


def enabled() -> bool:
    return _enabled


def enable() -> None:
    """Turn on background persistence; subsequent submits queue FIFO."""
    global _enabled, _atexit_registered
    with _lock:
        if _enabled:
            return
        _enabled = True
        _ensure_worker()
        if not _atexit_registered:
            atexit.register(_flush_at_exit)
            _atexit_registered = True


def disable(flush_first: bool = True) -> None:
    """Return to synchronous persistence (the default state).

    Pending jobs are flushed first unless ``flush_first`` is False, so a
    later synchronous save can never overwrite a queued one.
    """
    global _enabled
    with _lock:
        _enabled = False
    if flush_first:
        flush()


def submit(job: Callable[[], None]) -> None:
    """Queue ``job`` for the worker; runs inline while disabled.

    The inline fallback keeps callers branch-free: tests and the CLI get
    exactly the old synchronous behaviour.
    """
    global _pending
    if not _enabled:
        job()
        return
    with _lock:
        _ensure_worker()
        _pending += 1
        _idle.clear()
        if _queue is not None:
            _queue.put(job)


def flush(timeout: float = 5.0) -> bool:
    """Wait (bounded) for queued jobs to finish. True when drained."""
    if _idle.is_set():
        return True
    return _idle.wait(timeout)


def _ensure_worker() -> None:
    """Start the worker thread if it is not running. Caller holds ``_lock``."""
    global _queue, _worker
    if _worker is not None and _worker.is_alive():
        return
    q: queue.Queue[object] = queue.Queue()
    _queue = q
    _worker = threading.Thread(
        target=_worker_loop, args=(q,), name="flint-writeback", daemon=True
    )
    _worker.start()


def _worker_loop(q: "queue.Queue[object]") -> None:
    global _pending
    while True:
        item = q.get()
        try:
            if item is None:
                return
            item()  # type: ignore[operator]
        except Exception:
            # A failing save must never kill the worker: log and keep
            # draining (the next job still runs).
            logger.exception("writeback: job failed")
        finally:
            with _lock:
                _pending -= 1
                if _pending <= 0:
                    _idle.set()


def _flush_at_exit() -> None:
    if not flush(5.0):
        logger.warning("writeback: pending saves not flushed at exit")
