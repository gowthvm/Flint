"""Durable deployment job manifests and resume validation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("flint")

JOB_SCHEMA_VERSION = 1

# L10: only terminal states are garbage; anything still in flight is kept.
PRUNEABLE_STATES = frozenset({"written", "failed"})


@dataclass
class JobManifest:
    """Persisted identity and progress for one deployment target."""

    source_path: str
    source_size: int
    source_sha256: str
    target_fingerprint: str
    target_size: int
    options: dict[str, Any] = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "queued"
    checkpoint_bytes: int = 0
    error: str | None = None
    schema_version: int = JOB_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobManifest:
        if data.get("schema_version") != JOB_SCHEMA_VERSION:
            raise ValueError("unsupported deployment job schema")
        required = {
            "source_path",
            "source_size",
            "source_sha256",
            "target_fingerprint",
            "target_size",
        }
        if not required.issubset(data):
            raise ValueError("deployment job manifest is missing identity fields")
        # Unknown keys (e.g. older manifests carrying verification_bytes) are
        # ignored, so removing a field never breaks loading an old file.
        return cls(
            source_path=str(data["source_path"]),
            source_size=int(data["source_size"]),
            source_sha256=str(data["source_sha256"]),
            target_fingerprint=str(data["target_fingerprint"]),
            target_size=int(data["target_size"]),
            options=dict(data.get("options") or {}),
            job_id=str(data.get("job_id") or uuid.uuid4().hex),
            state=str(data.get("state") or "queued"),
            checkpoint_bytes=int(data.get("checkpoint_bytes") or 0),
            error=data.get("error"),
            schema_version=JOB_SCHEMA_VERSION,
        )

    def validate_resume(
        self,
        *,
        source_path: str,
        source_size: int,
        source_sha256: str,
        target_fingerprint: str,
        target_size: int,
        options: dict[str, Any],
        check_target_size: bool = True,
    ) -> None:
        """Raise ``ValueError`` unless the requested target is identical.

        ``check_target_size=False`` skips the capacity comparison. The
        detection layer reports capacity in whole GB (``size_gb``), so a
        freshly-scanned value can differ from the exact IOCTL size by up
        to 1 GB; callers that hold only a detection-rounded value must
        opt out rather than fail a resume that is otherwise identical.
        """
        checks = [
            (os.path.abspath(source_path), os.path.abspath(self.source_path), "source path"),
            (source_size, self.source_size, "source size"),
            (source_sha256, self.source_sha256, "source hash"),
            (target_fingerprint, self.target_fingerprint, "target identity"),
        ]
        if check_target_size:
            checks.append((target_size, self.target_size, "target size"))
        checks.append((options, self.options, "write options"))
        for actual, expected, label in checks:
            if actual != expected:
                raise ValueError(f"cannot resume: {label} changed")
        if not 0 <= self.checkpoint_bytes <= self.source_size:
            raise ValueError("cannot resume: checkpoint is outside the source image")


def source_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of a source image."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def save_manifest(path: str | Path, manifest: JobManifest) -> None:
    """Atomically persist a manifest beside its destination path."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_manifest(path: str | Path) -> JobManifest:
    """Load and validate a persisted manifest."""
    with open(path, "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise TypeError("deployment job manifest must be an object")
    return JobManifest.from_dict(data)


def prune_manifests(directory: str | Path, *, max_age_days: int = 30) -> int:
    """Delete stale, terminal-state manifests from *directory* (L10).

    A manifest is removed only when **both** hold:

    * its ``state`` is ``written`` or ``failed`` (active states —
      ``writing``/``resumable``/``queued`` — are never touched), and
    * its file mtime is at least ``max_age_days`` old.

    Files that cannot be parsed (or are not manifest objects) are skipped
    with a warning rather than deleted, because their state is unknown.
    Returns the number of manifests deleted; a missing directory returns 0.
    """
    root = Path(directory)
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in sorted(root.glob("*.json")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            logger.warning("jobs: cannot stat manifest %s; skipping", path.name)
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning(
                "jobs: unreadable manifest %s; leaving it in place", path.name
            )
            continue
        if not isinstance(data, dict) or "state" not in data:
            logger.warning(
                "jobs: %s is not a manifest object; leaving it in place",
                path.name,
            )
            continue
        state = str(data.get("state") or "")
        if state not in PRUNEABLE_STATES:
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("jobs: could not delete manifest %s", path.name)
            continue
        removed += 1
        logger.info(
            "jobs: pruned stale manifest %s (state=%s, age>=%sd)",
            path.name,
            state,
            max_age_days,
        )
    if removed:
        logger.info("jobs: pruned %d manifest(s) from %s", removed, root)
    return removed