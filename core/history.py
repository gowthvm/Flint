import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("flint")

_APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "Flint"
HISTORY_PATH = _APP_DIR / "history.json"
SCHEMA_VERSION = 2


def load_history() -> list[dict[str, Any]]:
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return [e for e in data["entries"] if isinstance(e, dict)]
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_history(entries: list[dict[str, Any]]) -> None:
    try:
        _APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "entries": entries,
        }
        tmp = HISTORY_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(HISTORY_PATH)
    except OSError:
        # A history write must never crash a flash flow or block close:
        # log and continue with the in-memory entry.
        logger.exception("failed to persist history")


def append_history(entry: dict[str, Any]) -> None:
    entries = load_history()
    entries.append(entry)
    save_history(entries)


def clear_history() -> None:
    save_history([])


def export_history(target_path: str | Path) -> bool:
    try:
        load_history()  # validates the store is readable
        shutil.copy2(HISTORY_PATH, target_path)
        return True
    except OSError:
        return False


def export_history_csv(target_path: str | Path) -> bool:
    """Export flash history as a CSV file."""
    import csv

    try:
        entries = load_history()
        if not entries:
            return False
        with open(target_path, "w", newline="", encoding="utf-8") as f:
            if not entries:
                return True
            writer = csv.DictWriter(f, fieldnames=list(entries[0].keys()))
            writer.writeheader()
            writer.writerows(entries)
        return True
    except (OSError, csv.Error):
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
        # Guard against hand-edited imports: non-dict entries would crash
        # rendering, so drop them rather than import the whole file.
        entries = [e for e in entries if isinstance(e, dict)]
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
    wipe_verified: str | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
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
        "wipe_verified": wipe_verified,
    }