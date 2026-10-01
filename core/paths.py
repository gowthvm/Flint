"""Canonical application directory and file-locking utilities for Flint.

All modules that need the per-user data directory should import
``APP_DIR`` from here rather than computing it independently.
"""

import contextlib
import json
import logging
import os
import tempfile
import time as _time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("flint")

APP_DIR: Path = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"


def atomic_write_text(path: Path, text: str) -> None:
    """Publish ``text`` at ``path`` atomically (fsync + same-dir replace).

    H2: a fixed ``path.with_suffix('.tmp')`` name is shared by every
    process, so two Flint instances writing at the same time truncate each
    other's temporary file and one of them publishes a torn JSON document.
    ``mkstemp`` gives each writer its own file in the destination directory
    (same volume, so ``os.replace`` is atomic); the loser's temp file is
    removed on failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def quarantine_corrupt(path: Path) -> Path | None:
    """Move an unreadable store aside instead of overwriting it.

    Returns the new path, or ``None`` when the rename failed. Callers
    treat a corrupt store as empty, so without this the next append would
    publish a fresh file on top of the damaged one and the bytes that
    might still be recoverable would be gone forever.
    """
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    # Never clobber an earlier quarantine from the same second.
    counter = 0
    while dest.exists():
        counter += 1
        dest = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
    try:
        path.replace(dest)
    except OSError:
        logger.exception("could not quarantine unreadable %s", path)
        return None
    logger.error(
        "unreadable %s preserved as %s; continuing with an empty store",
        path,
        dest,
    )
    return dest


def read_json_or_quarantine(path: Path) -> tuple[Any, bool]:
    """Return ``(data, corrupt)`` for a JSON store.

    ``corrupt`` is ``True`` when the file exists but could not be parsed -
    in that case ``data`` is ``None``. A missing file is *not* corrupt: it
    yields ``(None, False)`` and simply means "nothing stored yet".

    A ``JSONDecodeError`` means the bytes are permanently damaged, so they
    are moved aside first and can be inspected later. Any other ``OSError``
    may be transient (a lock, an antivirus handle) and is only logged: no
    rename is attempted, but the caller still must not treat the file as an
    empty store and write over it.
    """
    if not path.is_file():
        return None, False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not a JSONDecodeError: a file
        # full of binary noise must be quarantined too, not escape as an
        # unhandled exception from a plain read.
        logger.error("%s could not be decoded (%s: %s)", path,
                     type(exc).__name__, exc)
        quarantine_corrupt(path)
        return None, True
    except OSError as exc:
        logger.error("cannot read %s (%s)", path, exc)
        return None, True
    if parsed is None:
        # The file exists and decodes to JSON null - not "no file", and
        # not a usable store either.
        logger.error("%s contains JSON null", path)
        quarantine_corrupt(path)
        return None, True
    return parsed, False



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
