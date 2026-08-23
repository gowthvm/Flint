import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("flint")

_APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"
SETTINGS_PATH = _APP_DIR / "settings.json"

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
            return merged
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_DEFAULTS)

# In-memory cache to avoid repeated disk reads.
_CACHE: dict[str, Any] | None = None


def _ensure_loaded() -> dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE


def get(key: str) -> Any:
    data = _ensure_loaded()
    return data.get(key, _DEFAULTS.get(key))


def set_many(**values: Any) -> None:
    """Update settings in-memory and persist atomically to disk.

    Persistence failures (read-only appdata dir, missing permissions, disk
    errors) are logged and swallowed: the in-memory values still apply for
    this session, and a later save may succeed. Callers never crash or
    block close on a settings write.
    """
    data = _ensure_loaded()
    data.update(values)
    try:
        _APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(SETTINGS_PATH)
    except OSError:
        logger.exception("failed to persist settings")


def export_settings(target_path: str | Path) -> bool:
    """Export current settings to a JSON file."""
    try:
        data = _ensure_loaded()
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
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