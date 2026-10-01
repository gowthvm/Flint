import json
import logging
import threading
from typing import Any

from core import writeback
from core.paths import (
    APP_DIR,
    atomic_write_text,
    file_lock,
    quarantine_corrupt,
    read_json_or_quarantine,
)

logger = logging.getLogger("flint")

SETTINGS_PATH = APP_DIR / "settings.json"
_LOCK_PATH = SETTINGS_PATH.with_suffix(".lock")
_lock = threading.RLock()

# C10: settings.json is versioned so future formats can be migrated
# deterministically instead of relying only on per-key type checks.
SETTINGS_SCHEMA_VERSION = 1

_DEFAULTS: dict[str, Any] = {
    "theme": "dark",
    "verify_after_write": True,
    "window_geometry": None,
    "crash_report_seen_bytes": 0,
    "last_iso_dir": "",
    "onboarding_seen": False,
    "ask_before_elevation": True,
    "expert_mode": True,
    "close_to_tray": False,
    "partition_scheme": "auto",
    "target_system": "auto",
    "filesystem": "fat32",
    "write_mode": "auto",
    "chunk_size_mb": 8,
    "native_writer": False,
    "verify_sha256": True,
    "bad_block_scan": False,
    "bad_block_retries": 3,
    "log_level": "INFO",
    "auto_eject": False,
    "max_history_entries": 10_000,
    # U21: fleet "skip already-flashed" toggle (UI reads/writes this key).
    "fleet_skip_flashed": False,
    # U09: recent image selections offered back in the drop zone.
    "recent_images": [],
    "schema_version": SETTINGS_SCHEMA_VERSION,
}

# Settings whose values must have a specific type. Corrupted or hand-edited
# values are dropped on load so they fall back to the default instead of
# crashing startup (e.g. window_geometry of the wrong type).
_TYPE_CHECK: dict[str, type] = {
    "window_geometry": str,
    "theme": str,
    "verify_after_write": bool,
    "crash_report_seen_bytes": int,
    "last_iso_dir": str,
    "onboarding_seen": bool,
    "ask_before_elevation": bool,
    "expert_mode": bool,
    "close_to_tray": bool,
    "partition_scheme": str,
    "target_system": str,
    "filesystem": str,
    "write_mode": str,
    "chunk_size_mb": int,
    "native_writer": bool,
    "verify_sha256": bool,
    "bad_block_scan": bool,
    "bad_block_retries": int,
    "log_level": str,
    "auto_eject": bool,
    "max_history_entries": int,
    "fleet_skip_flashed": bool,
    "recent_images": list,
    "schema_version": int,
}

# Range / semantic validators.  Each callable receives the proposed value
# and returns True if acceptable.  Validators run *after* type checking.
_VALIDATORS: dict[str, Any] = {
    "chunk_size_mb": lambda v: isinstance(v, int) and 1 <= v <= 1024,
    "bad_block_retries": lambda v: isinstance(v, int) and 0 <= v <= 100,
    "crash_report_seen_bytes": lambda v: isinstance(v, int) and v >= 0,
    "max_history_entries": lambda v: isinstance(v, int) and 100 <= v <= 100_000,
}


# C10 migration dispatch: keyed by the schema version being migrated *from*
# (0 = files written before settings.json was versioned).  Each step takes a
# settings dict and returns the dict upgraded by one version.
def _migrate_v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Pre-versioned settings: the defaults-merge already supplies every key
    that v1 introduced, so no per-key rewriting is needed."""
    return data


_MIGRATIONS: dict[int, Any] = {0: _migrate_v0_to_v1}


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring a stored settings dict up to ``SETTINGS_SCHEMA_VERSION``.

    Missing or hand-corrupted versions are treated as 0 (pre-versioned).
    A dict from a *newer* Flint runs no migration and keeps its own
    version marker - stamping it down to ours would make the next upgrade
    re-run a migration over data that has already been migrated. Its
    unknown keys survive the defaults-merge that follows, so loading stays
    backward- and forward-tolerant.
    """
    raw = data.get("schema_version")
    version = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    if version > SETTINGS_SCHEMA_VERSION:
        return data
    while version < SETTINGS_SCHEMA_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            break
        data = step(data)
        version += 1
    data["schema_version"] = SETTINGS_SCHEMA_VERSION
    return data


class _Unreadable(Exception):
    """settings.json exists but could not be returned as a settings dict."""


def _load() -> dict[str, Any]:
    """Parse settings.json.

    A missing file is not an error - it is a first run. A file that exists
    but cannot be decoded raises :class:`_Unreadable` *after* being moved
    aside, so the caller can tell "nothing stored yet" apart from "the
    store is damaged" (S1): conflating the two would let the next save
    write defaults over settings that were still on disk.
    """
    data, corrupt = read_json_or_quarantine(SETTINGS_PATH)
    if corrupt:
        raise _Unreadable(str(SETTINGS_PATH))
    if data is None:
        return dict(_DEFAULTS)
    if not isinstance(data, dict):
        logger.error("settings: %s is not a JSON object", SETTINGS_PATH)
        quarantine_corrupt(SETTINGS_PATH)
        raise _Unreadable(str(SETTINGS_PATH))
    data = _migrate(data)
    merged = dict(_DEFAULTS)
    merged.update(data)
    for key, typ in _TYPE_CHECK.items():
        if key in merged and not isinstance(merged[key], typ):
            merged[key] = _DEFAULTS.get(key)
    for key, validator in _VALIDATORS.items():
        if key in merged and not validator(merged[key]):
            merged[key] = _DEFAULTS.get(key)
    return merged


def _load_or_defaults() -> dict[str, Any]:
    try:
        return _load()
    except _Unreadable:
        logger.error(
            "settings: %s is unreadable; running with defaults for now",
            SETTINGS_PATH,
        )
        return dict(_DEFAULTS)


# In-memory cache to avoid repeated disk reads.
_CACHE: dict[str, Any] | None = None


def _ensure_loaded() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _lock:
        if _CACHE is None:
            # S1: cache whatever we could read (defaults when the store is
            # unreadable). _persist_snapshot refuses to write over an
            # unreadable file, so a damaged store is never replaced by the
            # defaults we are running on.
            _CACHE = _load_or_defaults()
        return _CACHE


def get(key: str) -> Any:
    with _lock:
        data = _ensure_loaded()
        return data.get(key, _DEFAULTS.get(key))


def set_many(**values: Any) -> None:
    """Update settings in-memory and persist atomically to disk.

    Uses a threading lock for in-process safety and a file lock for
    inter-process (multi-instance) safety. Invalid values are rejected.
    Persistence failures are logged and swallowed.

    The in-memory cache is updated synchronously; the durable write runs
    on the caller's thread, or on the background write-back worker when
    ``writeback`` is enabled (GUI only, U15).
    """
    with _lock:
        data = _ensure_loaded()
        clean: dict[str, Any] = {}
        for key, val in values.items():
            expected = _TYPE_CHECK.get(key)
            if expected is not None and not isinstance(val, expected):
                logger.warning(
                    "settings: rejecting %s = %r (expected %s)",
                    key,
                    val,
                    expected.__name__,
                )
                continue
            validator = _VALIDATORS.get(key)
            if validator is not None and not validator(val):
                logger.warning(
                    "settings: rejecting %s = %r (out of valid range)",
                    key,
                    val,
                )
                continue
            clean[key] = val
        if not clean:
            return
        data.update(clean)
        # S2: persist only the keys this call changed. A full snapshot
        # overwrote every key on disk, which made the read-merge-write in
        # _persist_snapshot a no-op - a concurrent process's change to any
        # untouched key was always clobbered by ours.
        snapshot = dict(clean)
    try:
        if writeback.enabled():
            # U15: the cache above already reflects the new values, so
            # callers see them immediately; only the fsync+replace runs
            # off-thread (FIFO with every other queued save).
            writeback.submit(lambda: _persist_snapshot(snapshot))
        else:
            _persist_snapshot(snapshot)
    except OSError:
        logger.exception("failed to persist settings")


def _persist_snapshot(snapshot: dict[str, Any]) -> None:
    """Merge the changed keys in ``snapshot`` into the on-disk file.

    Runs on the caller's thread (CLI, tests) or on the write-back worker
    when ``writeback`` is enabled; ordering is FIFO either way. The
    read-merge-write happens under the inter-process file lock so a change
    made by another Flint instance to a key this call did not touch
    survives.
    """
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with file_lock(_LOCK_PATH):
        # Re-read from disk under file lock to merge with any
        # changes written by another process since our last load.
        try:
            disk_data = _load()
        except _Unreadable:
            if SETTINGS_PATH.is_file():
                # Quarantine failed earlier: the damaged bytes are still
                # there. Refuse rather than replace them with defaults.
                logger.error(
                    "settings: refusing to overwrite unreadable %s",
                    SETTINGS_PATH,
                )
                return
            disk_data = dict(_DEFAULTS)
        disk_data.update(snapshot)
        # C10: every save stamps at least the current schema version -
        # but never talks a file written by a *newer* Flint down, which
        # would make that file's next upgrade re-run a migration over
        # data that has already been migrated.
        stored = disk_data.get("schema_version")
        stored = stored if isinstance(stored, int) else SETTINGS_SCHEMA_VERSION
        disk_data["schema_version"] = max(stored, SETTINGS_SCHEMA_VERSION)
        # H2: a fixed .tmp name is shared with every other Flint process,
        # so two writers could publish a torn document.
        atomic_write_text(SETTINGS_PATH, json.dumps(disk_data, indent=2))
    # Only adopt the merged view once the bytes really landed: if the
    # write threw, the in-memory cache still holds what the caller set and
    # is closer to the truth than the file is.
    with _lock:
        global _CACHE
        _CACHE = disk_data
