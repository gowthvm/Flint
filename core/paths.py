"""Canonical application directory and file-locking utilities for Flint.

All modules that need the per-user data directory should import
``APP_DIR`` from here rather than computing it independently.
"""

import contextlib
import logging
import os
import time as _time
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger("flint")

APP_DIR: Path = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"


def _try_lock(fd: int, retries: int, delay: float) -> bool:
    """Attempt an msvcrt byte-range lock on *fd*; never raises.

    Returns ``True`` when the lock was acquired.  Returns ``False`` (after
    logging a warning) when locking is unavailable — non-Windows platforms
    where ``msvcrt`` does not import — or when every retry is exhausted
    because another process holds the lock.
    """
    for attempt in range(retries + 1):
        try:
            import msvcrt
        except ImportError:
            logger.warning(
                "file_lock: msvcrt unavailable on this platform; "
                "proceeding without a lock"
            )
            return False
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if attempt == retries:
                logger.warning(
                    "file_lock: could not lock fd %d after %d attempt(s); "
                    "proceeding without a lock",
                    fd,
                    retries + 1,
                )
                return False
            _time.sleep(delay)
    return False


@contextlib.contextmanager
def file_lock(
    path: Path,
    *,
    retries: int = 3,
    delay: float = 0.1,
) -> Iterator[None]:
    """Acquire an exclusive inter-process lock via a .lock sentinel file.

    Retries up to ``retries`` times with ``delay`` seconds between attempts.
    Degrades to a no-op if locking is unavailable (e.g. on non-Windows) or
    if the lock cannot be taken after the retries are exhausted: a warning is
    logged and the body runs **without** the lock instead of raising.

    Because a lost lock race can therefore interleave two writers, callers
    must additionally rely on the atomic-replace mitigation they already
    use: data is written to a temporary file in the same directory and
    published with ``os.replace``/``Path.replace``, so a reader ever sees a
    complete old or new file — never a torn one.
    """
    fd: int | None = None
    locked = False
    try:
        try:
            path.touch(exist_ok=True)
            fd = os.open(str(path), os.O_RDWR)
        except OSError as exc:
            logger.warning(
                "file_lock: cannot prepare lock file %s (%s); "
                "proceeding without a lock",
                path,
                exc,
            )
            fd = None
        if fd is not None:
            locked = _try_lock(fd, retries, delay)
        yield
    finally:
        if fd is not None:
            if locked:
                try:
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except (OSError, ImportError):
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
