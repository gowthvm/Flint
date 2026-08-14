import json
import os
from pathlib import Path
from typing import Any

_APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"
SETTINGS_PATH = _APP_DIR / "settings.json"

_DEFAULTS: dict[str, Any] = {
    "theme": "dark",
    "verify_after_write": True,
    "window_geometry": None,
    "crash_report_seen_bytes": 0,
    "last_iso_dir": "",
    "tray_hint_seen": False,
    "onboarding_seen": False,
    "ask_before_elevation": True,
    "expert_mode": True,
    "partition_scheme": "auto",
    "target_system": "auto",
    "filesystem": "fat32",
    "write_mode": "auto",
    "chunk_size_mb": 8,
    "native_writer": False,
    "verify_sha256": True,
    "bad_block_scan": False,
    "bad_block_retries": 3,
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
    "tray_hint_seen": bool,
    "onboarding_seen": bool,
    "ask_before_elevation": bool,
    "expert_mode": bool,
    "partition_scheme": str,
    "target_system": str,
    "filesystem": str,
    "write_mode": str,
    "chunk_size_mb": int,
    "native_writer": bool,
    "verify_sha256": bool,
    "bad_block_scan": bool,
    "bad_block_retries": int,
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
    """Update settings in-memory and persist atomically to disk."""
    _APP_DIR.mkdir(parents=True, exist_ok=True)
    data = _ensure_loaded()
    data.update(values)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(SETTINGS_PATH)