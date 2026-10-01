"""History store: JSON file on disk with a small in-process read cache.

Cache contract (U13): repeated ``load_history()`` calls do not re-parse the
file while it is unchanged.  The cache is keyed by ``(path, mtime_ns,
size)`` -- the path is part of the key because tests (and a relocated
install) reassign ``HISTORY_PATH`` -- and is invalidated by every write.
The cache is a plain module global, not a lock: history is read and
written from the GUI thread / CLI main thread only, and any cross-thread
use would have to be serialised by the caller.
"""

import hashlib
import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
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

HISTORY_PATH = APP_DIR / "history.json"
_HISTORY_LOCK_PATH = HISTORY_PATH.with_suffix(".lock")
SCHEMA_VERSION = 2
_MAX_HISTORY_ENTRIES = 10_000

_HistoryKey = tuple[str, int, int] | None
_history_cache: tuple[_HistoryKey, list[dict[str, Any]]] | None = None

# U15: entries for an async save that has not landed on disk yet, as
# ``(sequence, entries)``.  Readers see the pending list instead of the
# file, so a save is immediately visible even though the fsync runs on
# the background worker.  The sequence lets an older job avoid clearing
# a newer pending list.
_pending_entries: tuple[int, list[dict[str, Any]]] | None = None
_pending_seq = 0
_pending_lock = threading.Lock()


def _set_pending(entries: list[dict[str, Any]]) -> int:
    global _pending_entries, _pending_seq
    with _pending_lock:
        _pending_seq += 1
        seq = _pending_seq
        _pending_entries = (seq, entries)
    return seq


def _clear_pending(seq: int) -> None:
    global _pending_entries
    with _pending_lock:
        if _pending_entries is not None and _pending_entries[0] == seq:
            _pending_entries = None


def _history_key(path: Path) -> _HistoryKey:
    """Cheap freshness probe; ``None`` means "no file yet"."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _invalidate_history_cache() -> None:
    global _history_cache
    _history_cache = None


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
    """Return the stored entries, re-parsing the file only when it changed.

    The returned list is a fresh copy so callers may append/reorder it
    freely (e.g. before ``save_history``); the cached list itself is never
    handed out.  Entry dicts are shared, so callers must not mutate them
    in place without saving afterwards.

    While an async save is pending (U15), the pending entries are
    returned — they are newer than anything on disk.
    """
    global _history_cache
    pending = _pending_entries
    if pending is not None:
        return list(pending[1])
    key = _history_key(HISTORY_PATH)
    cached = _history_cache
    if cached is not None and cached[0] == key:
        return list(cached[1])
    entries, writable = _read_history(HISTORY_PATH)
    if not writable:
        # H1: a failed read must never be cached. The file key still
        # matches, so caching "empty" here would hide the damage from
        # every later call and make verify_history_integrity() pass
        # vacuously.
        _invalidate_history_cache()
        return []
    _history_cache = (key, entries)
    return list(entries)


def _read_history(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Parse the store; return ``(entries, safe_to_write_over)``.

    ``safe_to_write_over`` is ``False`` only when the file exists, could
    not be parsed, *and* could not be moved aside - publishing a fresh
    store on top of it would destroy the only copy (H1). A missing file is
    ``( [], True )``: nothing stored yet.
    """
    data, corrupt = read_json_or_quarantine(path)
    if corrupt:
        return [], not path.is_file()
    if data is None:
        return [], True
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return [e for e in data["entries"] if isinstance(e, dict)], True
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)], True
    # Valid JSON, wrong shape - just as unreadable as a truncated file.
    logger.error("history: %s has an unexpected shape", path)
    quarantine_corrupt(path)
    return [], not path.is_file()


def _save_history_unlocked(entries: list[dict[str, Any]]) -> bool:
    """Write history to disk. Caller must already hold the file lock.

    Returns ``False`` when the destination is unreadable and could not be
    quarantined; callers must then keep their in-memory state instead of
    reporting a successful save (H1).
    """
    if HISTORY_PATH.is_file() and not _read_history(HISTORY_PATH)[1]:
        logger.error(
            "history: refusing to overwrite unreadable %s", HISTORY_PATH
        )
        return False
    entries = _truncate_entries(entries)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": entries,
    }
    # H2: a fixed .tmp name is shared with every other Flint process, so
    # two writers could publish a torn document. atomic_write_text gives
    # each writer its own temporary file.
    atomic_write_text(HISTORY_PATH, json.dumps(payload, indent=2))
    # The file on disk changed: drop the read cache so the next
    # ``load_history`` reflects what was just written (and anything a
    # concurrent reader wrote while we held the lock).
    _invalidate_history_cache()
    return True


def save_history(entries: list[dict[str, Any]]) -> None:
    """Persist history entries atomically, protected by a file lock.

    With ``writeback`` enabled (GUI, U15) the entries become visible to
    readers immediately and the fsync runs on the background worker.
    """
    if writeback.enabled():
        seq = _set_pending(entries)
        writeback.submit(lambda: _persist_entries(entries, seq))
        return
    _persist_entries(entries)


def _persist_entries(entries: list[dict[str, Any]], seq: int | None = None) -> None:
    """Write ``entries`` under the file lock; clear pending on success.

    When ``seq`` is given the caller was async: the pending list is only
    dropped once the entries are actually on disk, so a failed write
    keeps serving the in-memory state to readers.
    """
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            saved = _save_history_unlocked(entries)
        if seq is not None and saved:
            # H1: only drop the overlay once the bytes really landed. A
            # refused save (unreadable destination) leaves readers on the
            # in-memory state instead of silently reverting them to disk.
            _clear_pending(seq)
    except OSError:
        # A history write must never crash a flash flow or block close:
        # log and continue with the in-memory entry.
        logger.exception("failed to persist history")


def append_history(entry: dict[str, Any]) -> None:
    """Append a single entry to history, protected by a file lock."""
    if writeback.enabled():
        # U15: merge on this thread (cheap — no fsync), durable write
        # off-thread.  The worker re-reads the file so a concurrent
        # CLI save is never overwritten by a stale list.
        entries = load_history()
        entries.append(entry)
        seq = _set_pending(entries)
        writeback.submit(lambda: _append_on_worker(entry, seq))
        return
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            entries = load_history()
            entries.append(entry)
            _save_history_unlocked(entries)
    except OSError:
        logger.exception("failed to append history")


def _append_on_worker(entry: dict[str, Any], seq: int) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            # Bypass the pending overlay: disk is the merge base here.
            entries, writable = _read_history(HISTORY_PATH)
            if not writable:
                logger.error(
                    "history: append skipped, %s is unreadable", HISTORY_PATH
                )
                return
            entries.append(entry)
            saved = _save_history_unlocked(entries)
        if saved:
            _clear_pending(seq)
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


def _make_record(
    entry: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Hash-chained copy of ``entry`` given the current ``entries`` tail."""
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
    return record


def append_audited_history(entry: dict[str, Any]) -> dict[str, Any]:
    """Append a hash-chained operation record and return the stored record."""
    if writeback.enabled():
        # U15: chain and expose the record here (callers get it back
        # immediately), fsync off-thread.  The chain is computed against
        # the overlay view; a concurrent cross-process append inside the
        # few-millisecond window would need two writers at once (CLI +
        # GUI flashing together) — accepted, and noted for the register.
        entries = load_history()
        record = _make_record(entry, entries)
        seq = _set_pending([*entries, record])
        writeback.submit(lambda: _append_record_on_worker(record, seq))
        return record
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            entries = load_history()
            record = _make_record(entry, entries)
            entries.append(record)
            if not _save_history_unlocked(entries):
                return entry
            return record
    except OSError:
        logger.exception("failed to append audited history")
        return entry


def _append_record_on_worker(record: dict[str, Any], seq: int) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with file_lock(_HISTORY_LOCK_PATH):
            entries, writable = _read_history(HISTORY_PATH)
            if not writable:
                logger.error(
                    "history: append skipped, %s is unreadable", HISTORY_PATH
                )
                return
            entries.append(record)
            saved = _save_history_unlocked(entries)
        if saved:
            _clear_pending(seq)
    except OSError:
        logger.exception("failed to append audited history")


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
    writeback.flush(5.0)  # land pending async saves first (U15)
    try:
        load_history()  # validates the store is readable
        target = Path(target_path)
        tmp = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(HISTORY_PATH, tmp)
        tmp.replace(target)
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
        # C2: entries are not uniform - an audited record carries
        # integrity_prev/integrity_sha256 that a plain flash record does
        # not, and a wipe has no iso. DictWriter raises ValueError on any
        # field outside fieldnames, so take the union (as the Markdown
        # export already does) instead of only the first record's keys.
        fieldnames = list(dict.fromkeys(key for e in entries for key in e))
        with open(target_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(entries)
        return True
    except (OSError, csv.Error, ValueError):
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
    writeback.flush(5.0)  # land pending async saves first (U15)
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
    *,
    operation: str = "flash",
    drive_path: str | None = None,
) -> dict[str, Any]:
    """Build a history record.

    ``operation`` lets the caller label the record (``"flash"``, ``"wipe"``,
    ``"backup"``...) instead of post-assigning ``report["operation"]`` after
    the dict is built.  ``drive_path`` records the physical device path so
    a stick that reports no serial can still be recognised later
    (see ``core.fleet.was_recently_flashed``).
    """
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "operation": operation,
        "iso": iso_name,
        "drive": drive_model,
        "drive_serial": drive_serial,
        "physical_path": drive_path,
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
