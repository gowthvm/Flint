import json
import os
from pathlib import Path

_APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"
SETTINGS_PATH = _APP_DIR / "settings.json"

_DEFAULTS: dict = {
    "theme": "dark",
    "verify_after_write": True,
    "window_geometry": None,
    "crash_report_seen_bytes": 0,
    "last_iso_dir": "",
    "tray_hint_seen": False,
    "onboarding_seen": False,
    "ask_before_elevation": True,
    "expert_mode": False,
    "partition_scheme": "auto",
    "target_system": "auto",
    "filesystem": "fat32",
    "write_mode": "auto",
}


def _load() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged = dict(_DEFAULTS)
            merged.update(data)
            return merged
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_DEFAULTS)

# In-memory cache to avoid repeated disk reads.
_CACHE: dict | None = None


def _ensure_loaded() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE


def get(key: str):
    data = _ensure_loaded()
    return data.get(key, _DEFAULTS.get(key))


def set_many(**values) -> None:
    """Update settings in-memory and persist atomically to disk."""
    _APP_DIR.mkdir(parents=True, exist_ok=True)
    data = _ensure_loaded()
    data.update(values)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(SETTINGS_PATH)