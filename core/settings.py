import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from core.paths import APP_DIR, file_lock

logger = logging.getLogger("flint")

SETTINGS_PATH = APP_DIR / "settings.json"
_LOCK_PATH = SETTINGS_PATH.with_suffix(".lock")
_lock = threading.RLock()

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
}

# Range / semantic validators.  Each callable receives the proposed value
# and returns True if acceptable.  Validators run *after* type checking.
_VALIDATORS: dict[str, Any] = {
    "chunk_size_mb": lambda v: isinstance(v, int) and 1 <= v <= 1024,
    "bad_block_retries": lambda v: isinstance(v, int) and 0 <= v <= 100,
    "crash_report_seen_bytes": lambda v: isinstance(v, int) and v >= 0,
    "max_history_entries": lambda v: isinstance(v, int) and 100 <= v <= 100_000,
}


def _load() -> dict[str, Any]:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged = dict(_DEFAULTS)
            merged.update(data)
            for key, typ in _TYPE_CHECK.items():
                if key in merged and not isinstance(merged[key], typ):
                    merged[key] = _DEFAULTS.get(key)
            for key, validator in _VALIDATORS.items():
                if key in merged and not validator(merged[key]):
                    merged[key] = _DEFAULTS.get(key)
            return merged
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_DEFAULTS)


# In-memory cache to avoid repeated disk reads.
_CACHE: dict[str, Any] | None = None


def _ensure_loaded() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _lock:
        if _CACHE is None:
            _CACHE = _load()
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
        snapshot = dict(data)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_LOCK_PATH):
            # Re-read from disk under file lock to merge with any
            # changes written by another process since our last load.
            disk_data = _load()
            disk_data.update(snapshot)
            with _lock:
                global _CACHE
                _CACHE = disk_data
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(disk_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(SETTINGS_PATH)
    except OSError:
        logger.exception("failed to persist settings")


def export_settings(target_path: str | Path) -> bool:
    """Export current settings to a JSON file."""
    try:
        with _lock:
            data = _ensure_loaded()
            snapshot = dict(data)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        return True
    except OSError:
        return False


def import_settings(source_path: str | Path) -> tuple[bool, int]:
    """Import settings from a JSON file. Returns (ok, count_imported)."""
    try:
        with open(source_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False, 0
        valid = {k: v for k, v in data.items() if k in _DEFAULTS}
        set_many(**valid)
        return True, len(valid)
    except (OSError, json.JSONDecodeError):
        return False, 0
