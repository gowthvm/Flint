import json
import os
import shutil
from datetime import datetime
from pathlib import Path

_APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"
HISTORY_PATH = _APP_DIR / "history.json"
SCHEMA_VERSION = 2


def load_history() -> list[dict]:
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data["entries"]
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_history(entries: list[dict]) -> None:
    _APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "entries": entries,
    }
    tmp = HISTORY_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(HISTORY_PATH)


def append_history(entry: dict) -> None:
    entries = load_history()
    entries.append(entry)
    save_history(entries)


def clear_history() -> None:
    save_history([])


def export_history(target_path: str | Path) -> bool:
    try:
        entries = load_history()
        shutil.copy2(HISTORY_PATH, target_path)
        return True
    except OSError:
        return False


def import_history(source_path: str | Path) -> tuple[bool, int]:
    try:
        with open(source_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            entries = data.get("entries")
            if not isinstance(entries, list):
                return False, 0
        elif isinstance(data, list):
            entries = data
        else:
            return False, 0
        save_history(entries)
        return True, len(entries)
    except (OSError, json.JSONDecodeError):
        return False, 0


def flash_report(
    iso_name: str,
    drive_model: str,
    duration_seconds: float,
    verified: bool,
    success: bool,
    iso_sha256: str | None = None,
    written_sha256: str | None = None,
    drive_serial: str | None = None,
    bootable: str | None = None,
    avg_mbps: float | None = None,
) -> dict:
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "iso": iso_name,
        "drive": drive_model,
        "drive_serial": drive_serial,
        "duration": round(duration_seconds, 1),
        "avg_mbps": round(avg_mbps, 1) if avg_mbps is not None else None,
        "verified": bool(verified),
        "success": bool(success),
        "bootable": bootable,
        "iso_sha256": iso_sha256,
        "written_sha256": written_sha256,
    }