import hashlib
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from core.paths import APP_DIR, file_lock

logger = logging.getLogger("flint")

HISTORY_PATH = APP_DIR / "history.json"
_HISTORY_LOCK_PATH = HISTORY_PATH.with_suffix(".lock")
SCHEMA_VERSION = 2
_MAX_HISTORY_ENTRIES = 10_000


def _truncate_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim non-audited entries if history exceeds the size limit.

    Audited entries (containing ``integrity_sha256``) are always kept
    because removing one would break the hash chain.  Chronological
    order is preserved.
    """
    from core.settings import get

    max_entries = get("max_history_entries") or _MAX_HISTORY_ENTRIES
    if len(entries) <= max_entries:
        return entries

    audited_indices = {i for i, e in enumerate(entries) if e.get("integrity_sha256")}
    excess = len(entries) - max_entries
    # Drop the oldest non-audited entries first (indices from the start).
    drop: set[int] = set()
    for i in range(len(entries)):
        if len(drop) >= excess:
            break
        if i not in audited_indices:
            drop.add(i)
    trimmed = [e for i, e in enumerate(entries) if i not in drop]
    if len(trimmed) < len(entries):
        logger.info(
            "history truncated: %d -> %d entries (dropped %d non-audited)",
            len(entries),
            len(trimmed),
            len(entries) - len(trimmed),
        )
    return trimmed


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


def _save_history_unlocked(entries: list[dict[str, Any]]) -> None:
    """Write history to disk. Caller must already hold the file lock."""
    entries = _truncate_entries(entries)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": entries,
    }
    tmp = HISTORY_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(HISTORY_PATH)


def save_history(entries: list[dict[str, Any]]) -> None:
    """Persist history entries atomically, protected by a file lock."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            _save_history_unlocked(entries)
    except OSError:
        # A history write must never crash a flash flow or block close:
        # log and continue with the in-memory entry.
        logger.exception("failed to persist history")


def append_history(entry: dict[str, Any]) -> None:
    """Append a single entry to history, protected by a file lock."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            entries = load_history()
            entries.append(entry)
            _save_history_unlocked(entries)
    except OSError:
        logger.exception("failed to append history")


def _integrity_payload(entry: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in entry.items()
        if key not in {"integrity_prev", "integrity_sha256"}
    }
    return json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def history_entry_digest(entry: dict[str, Any]) -> str:
    """Return the deterministic digest used for an audited history entry."""
    return hashlib.sha256(_integrity_payload(entry).encode("utf-8")).hexdigest()


def append_audited_history(entry: dict[str, Any]) -> dict[str, Any]:
    """Append a hash-chained operation record and return the stored record."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            entries = load_history()
            record = dict(entry)
            previous = next(
                (
                    str(item["integrity_sha256"])
                    for item in reversed(entries)
                    if item.get("integrity_sha256")
                ),
                None,
            )
            if previous is not None:
                record["integrity_prev"] = previous
            record["integrity_sha256"] = history_entry_digest(record)
            entries.append(record)
            _save_history_unlocked(entries)
            return record
    except OSError:
        logger.exception("failed to append audited history")
        return entry


def verify_history_integrity() -> tuple[bool, int | None]:
    """Return ``(valid, index)`` for the first broken audited record."""
    previous: str | None = None
    for index, entry in enumerate(load_history()):
        digest = entry.get("integrity_sha256")
        if not digest:
            continue
        if entry.get("integrity_prev") != previous:
            return False, index
        if digest != history_entry_digest(entry):
            return False, index
        previous = str(digest)
    return True, None


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
            writer = csv.DictWriter(f, fieldnames=list(entries[0].keys()))
            writer.writeheader()
            writer.writerows(entries)
        return True
    except (OSError, csv.Error):
        return False


def export_history_markdown(target_path: str | Path) -> bool:
    """Export operation history as a readable Markdown audit table."""
    try:
        entries = load_history()
        keys = sorted({key for entry in entries for key in entry})
        if not keys:
            return False

        def cell(value: Any) -> str:
            return (
                str(value if value is not None else "")
                .replace("|", "\\|")
                .replace("\n", " ")
            )

        lines = ["# Flint Operation History", "", "| " + " | ".join(keys) + " |"]
        lines.append("| " + " | ".join("---" for _ in keys) + " |")
        for entry in entries:
            lines.append(
                "| " + " | ".join(cell(entry.get(key)) for key in keys) + " |"
            )
        Path(target_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
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
        # Guard against hand-edited imports: non-dict entries would crash
        # rendering, so drop them rather than import the whole file.
        entries = [e for e in entries if isinstance(e, dict)]
        # Back up existing history before replacing.
        if HISTORY_PATH.is_file():
            backup = HISTORY_PATH.with_suffix(".json.import-backup")
            try:
                import shutil

                shutil.copy2(HISTORY_PATH, backup)
            except OSError:
                pass
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
    boot_status: str | None = None,
    avg_mbps: float | None = None,
    wipe_verified: str | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "operation": "flash",
        "iso": iso_name,
        "drive": drive_model,
        "drive_serial": drive_serial,
        "duration": round(duration_seconds, 1),
        "avg_mbps": round(avg_mbps, 1) if avg_mbps is not None else None,
        "verified": bool(verified),
        "success": bool(success),
        "bootable": bootable,
        "boot_status": boot_status,
        "iso_sha256": iso_sha256,
        "written_sha256": written_sha256,
        "wipe_verified": wipe_verified,
    }
