"""One-click bug-report bundle: app version, drives, history and logs.

``build_diagnostics`` is intentionally pure (no Qt) so it is easy to test
and can be reused by the CLI later.
"""

import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from core.history import load_history
from core.version import APP_VERSION

_LOG_LIMIT = 200_000
_HISTORY_LIMIT = 20


def _read_tail(path: Path, limit: int = _LOG_LIMIT) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(data) > limit:
        data = data[-limit:]
    return data


def build_diagnostics(
    drives: list[dict[str, Any]],
    elevated: bool | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> str:
    """Compose a plain-text diagnostics bundle for an issue report."""
    lines: list[str] = []
    lines.append("Flint diagnostics")
    lines.append("=" * 40)
    lines.append(f"Version: {APP_VERSION}")
    lines.append(
        "Timestamp: "
        + datetime.now().astimezone().isoformat(timespec="seconds")
    )
    lines.append(f"Python: {sys.version.split()[0]}")
    lines.append(f"Platform: {platform.platform()}")
    lines.append(f"Elevated: {'yes' if elevated else 'no'}")
    lines.append("")

    lines.append(f"Drives ({len(drives)}):")
    for i, d in enumerate(drives, 1):
        letters = d.get("letters") or (
            [d["letter"]] if d.get("letter") else []
        )
        lines.append(
            f"  {i}. {d.get('model') or d.get('name')} | "
            f"{d.get('size_gb')} GB | letters={', '.join(letters)} | "
            f"bus={d.get('bus_type')} | serial={d.get('serial')} | "
            f"path={d.get('physical_path')}"
        )
    lines.append("")

    history = entries if entries is not None else load_history()
    recent = history[-_HISTORY_LIMIT:]
    lines.append(f"History (last {len(recent)} of {len(history)}):")
    for e in recent:
        lines.append(
            f"  {e.get('timestamp')} | {e.get('iso')} -> "
            f"{e.get('drive')} | success={e.get('success')} | "
            f"verified={e.get('verified')} | "
            f"wipe_verified={e.get('wipe_verified')}"
        )
    lines.append("")

    temp = Path(os.environ.get("TEMP", "."))
    for suffix in ("", ".1", ".2", ".3"):
        log = temp / f"flint-startup.log{suffix}"
        lines.append(f"--- {log} ---")
        lines.append(_read_tail(log))
        lines.append("")
    crash_dir = Path(os.environ.get("LOCALAPPDATA", str(temp))) / "Flint"
    crash = crash_dir / "crash.log"
    lines.append(f"--- {crash} ---")
    lines.append(_read_tail(crash))
    lines.append("")

    return "\n".join(lines)