"""Canonical application directory and file-locking utilities for Flint.

All modules that need the per-user data directory should import
``APP_DIR`` from here rather than computing it independently.
"""

import contextlib
import os
import time as _time
from collections.abc import Iterator
from pathlib import Path

APP_DIR: Path = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"


@contextlib.contextmanager
def file_lock(
    path: Path,
    *,
    retries: int = 3,
    delay: float = 0.1,
) -> Iterator[None]:
    """Acquire an exclusive inter-process lock via a .lock sentinel file.

    Retries up to ``retries`` times with ``delay`` seconds between attempts.
    Degrades to a no-op if locking is unavailable (e.g. on non-Windows).
    """
    path.touch(exist_ok=True)
    fd: int | None = None
    try:
        fd = os.open(str(path), os.O_RDWR)
        for attempt in range(retries + 1):
            try:
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if attempt == retries:
                    raise
                _time.sleep(delay)
        yield
    finally:
        if fd is not None:
            try:
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)
