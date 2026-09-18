"""Durable deployment job manifests and resume validation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

JOB_SCHEMA_VERSION = 1


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
    verification_bytes: int = 0
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
            verification_bytes=int(data.get("verification_bytes") or 0),
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
    ) -> None:
        """Raise ``ValueError`` unless the requested target is identical."""
        checks = (
            (os.path.abspath(source_path), os.path.abspath(self.source_path), "source path"),
            (source_size, self.source_size, "source size"),
            (source_sha256, self.source_sha256, "source hash"),
            (target_fingerprint, self.target_fingerprint, "target identity"),
            (target_size, self.target_size, "target size"),
            (options, self.options, "write options"),
        )
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