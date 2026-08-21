"""Fleet flashing: write the queued images to every drive that is plugged in.

A fleet session is never persisted: arming is an explicit, per-run action
that survives only while the app is open and expires after an hour without
any activity, so an unattended machine cannot silently keep flashing
drives forever.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

IDLE_EXPIRY_SECONDS = 60 * 60
BYTES_PER_GB = 1_000_000_000


@dataclass
class FleetSession:
    """One armed session: the images to write and what has been done."""

    images: list[str]
    arm_ts: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    done_serials: set[str] = field(default_factory=set)
    done_count: int = 0
    failed_count: int = 0

    def image_sizes(self) -> dict[str, int]:
        return {
            path: os.path.getsize(path)
            for path in self.images
            if os.path.isfile(path)
        }

    def fits_on_drive(self, image: str, drive: dict[str, Any]) -> bool:
        """The drive's reported capacity must hold the whole image.

        The picker reports sizes in decimal gigabytes (size_gb * 1e9),
        so fleet applies the same convention for the capacity check.
        """
        size = self.image_sizes().get(image)
        if size is None:
            return False
        capacity = drive.get("size_gb")
        if not isinstance(capacity, (int, float)):
            return False
        return size <= capacity * BYTES_PER_GB

    def mark_flashed(self, drive: dict[str, Any]) -> None:
        fp = drive_fingerprint(drive)
        if fp is not None:
            self.done_serials.add(fp)
        self.done_count += 1
        self.last_activity = time.monotonic()

    def mark_failed(self) -> None:
        self.failed_count += 1
        self.last_activity = time.monotonic()

    def expired(self, now: float | None = None) -> bool:
        """Idle expiry: armed but nothing happened for an hour."""
        return (now if now is not None else time.monotonic()) - self.last_activity > IDLE_EXPIRY_SECONDS


def drive_fingerprint(drive: dict[str, Any]) -> str | None:
    """Stable per-stick identity for a session.

    The serial is preferred; physical paths are stable enough for a
    session when the stick reports no serial at all.
    """
    serial = drive.get("serial")
    if serial:
        return str(serial)
    path = drive.get("physical_path")
    return str(path) if path else None


def was_recently_flashed(
    drive: dict[str, Any],
    image_path: str,
    *,
    window_hours: int = 24 * 365,
) -> bool:
    """Check if a drive was already successfully flashed with this image."""
    from core.history import load_history

    fp = drive_fingerprint(drive)
    if not fp:
        return False

    serial = drive.get("serial") or ""
    if not serial:
        return False

    image_name = os.path.basename(image_path)
    cutoff = datetime.now().astimezone() - timedelta(hours=window_hours)

    try:
        for entry in load_history():
            if not entry.get("success"):
                continue
            if (entry.get("drive_serial") or "") != serial:
                continue
            if entry.get("iso") != image_name:
                continue
            ts_str = entry.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=cutoff.tzinfo)
            if ts >= cutoff:
                return True
    except (TypeError, ValueError, KeyError, OSError):
        pass

    return False


def pick_candidate(
    drives: list[dict[str, Any]],
    session: FleetSession,
    now: float | None = None,
    skip_flashed: bool = False,
) -> dict[str, Any] | None:
    """First drive that has not been flashed yet and fits every image."""
    if session.expired(now):
        return None
    sizes = session.image_sizes()
    if not sizes or len(sizes) != len(session.images):
        return None
    for drive in drives:
        fp = drive_fingerprint(drive)
        if fp is not None and fp in session.done_serials:
            continue
        if all(
            session.fits_on_drive(image, drive) for image in session.images
        ):
            if skip_flashed and any(
                was_recently_flashed(drive, image)
                for image in session.images
            ):
                continue
            return drive
    return None