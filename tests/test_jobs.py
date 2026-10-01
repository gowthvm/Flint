import json
import os
import time
from dataclasses import asdict

import pytest

import core.jobs as jobs_mod
from core.jobs import (
    JobManifest,
    load_manifest,
    prune_manifests,
    save_manifest,
    source_sha256,
)


def _manifest(tmp_path):
    source = tmp_path / "image.iso"
    source.write_bytes(b"flint-image")
    return source, JobManifest(
        source_path=str(source),
        source_size=source.stat().st_size,
        source_sha256=source_sha256(source),
        target_fingerprint="serial:USB-1",
        target_size=32_000,
        options={"chunk_size": 4096, "verify": True},
    )


def test_manifest_round_trip_is_versioned_and_atomic(tmp_path):
    source, manifest = _manifest(tmp_path)
    path = tmp_path / "jobs" / "job.json"

    save_manifest(path, manifest)
    loaded = load_manifest(path)

    assert loaded == manifest
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert not list(path.parent.glob("*.tmp"))
    assert source_sha256(source) == manifest.source_sha256


def test_manifest_rejects_changed_source_or_target(tmp_path):
    _, manifest = _manifest(tmp_path)
    valid = {
        "source_path": manifest.source_path,
        "source_size": manifest.source_size,
        "source_sha256": manifest.source_sha256,
        "target_fingerprint": manifest.target_fingerprint,
        "target_size": manifest.target_size,
        "options": manifest.options,
    }
    manifest.validate_resume(**valid)

    with pytest.raises(ValueError, match="source hash changed"):
        manifest.validate_resume(**{**valid, "source_sha256": "0" * 64})
    with pytest.raises(ValueError, match="target identity changed"):
        manifest.validate_resume(**{**valid, "target_fingerprint": "serial:USB-2"})
    with pytest.raises(ValueError, match="write options changed"):
        manifest.validate_resume(**{**valid, "options": {"verify": False}})


def test_manifest_rejects_unsupported_schema(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported deployment job schema"):
        load_manifest(path)


def test_manifest_rejects_invalid_checkpoint(tmp_path):
    _, manifest = _manifest(tmp_path)
    manifest.checkpoint_bytes = manifest.source_size + 1
    valid = {
        "source_path": manifest.source_path,
        "source_size": manifest.source_size,
        "source_sha256": manifest.source_sha256,
        "target_fingerprint": manifest.target_fingerprint,
        "target_size": manifest.target_size,
        "options": manifest.options,
    }

    with pytest.raises(ValueError, match="checkpoint"):
        manifest.validate_resume(**valid)


def test_manifest_rejects_changed_target_size(tmp_path):
    _, manifest = _manifest(tmp_path)
    valid = {
        "source_path": manifest.source_path,
        "source_size": manifest.source_size,
        "source_sha256": manifest.source_sha256,
        "target_fingerprint": manifest.target_fingerprint,
        "target_size": manifest.target_size,
        "options": manifest.options,
    }

    with pytest.raises(ValueError, match="target size changed"):
        manifest.validate_resume(**{**valid, "target_size": manifest.target_size + 1})


def test_manifest_can_skip_target_size_check(tmp_path):
    """Detection rounds capacity to whole GB, so the fleet scanner opts out
    of the size comparison while every identity field stays enforced."""
    _, manifest = _manifest(tmp_path)
    valid = {
        "source_path": manifest.source_path,
        "source_size": manifest.source_size,
        "source_sha256": manifest.source_sha256,
        "target_fingerprint": manifest.target_fingerprint,
        "target_size": manifest.target_size,
        "options": manifest.options,
    }

    manifest.validate_resume(
        **{**valid, "target_size": manifest.target_size + 1},
        check_target_size=False,
    )

    with pytest.raises(ValueError, match="source hash changed"):
        manifest.validate_resume(
            **{**valid, "source_sha256": "0" * 64},
            check_target_size=False,
        )
    with pytest.raises(ValueError, match="target identity changed"):
        manifest.validate_resume(
            **{**valid, "target_fingerprint": "serial:USB-9"},
            check_target_size=False,
        )


# --- C03: dead API removal -------------------------------------------------


def test_manifest_has_no_verification_bytes_field(tmp_path):
    _, manifest = _manifest(tmp_path)
    assert "verification_bytes" not in asdict(manifest)


def test_manifest_still_loads_when_legacy_verification_bytes_present(tmp_path):
    """Old manifests on disk carry verification_bytes; from_dict ignores the
    unknown key instead of failing."""
    _, manifest = _manifest(tmp_path)
    payload = asdict(manifest)
    payload["verification_bytes"] = 12_345
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_manifest(path)

    assert loaded.job_id == manifest.job_id
    assert not hasattr(loaded, "verification_bytes")


# --- L10: prune_manifests --------------------------------------------------


def _write_manifest(
    directory, name: str, state: str, mtime: float | None = None, **overrides
):
    data = {
        "schema_version": 1,
        "source_path": "C:/images/source.iso",
        "source_size": 1024,
        "source_sha256": "0" * 64,
        "target_fingerprint": "serial:USB-1",
        "target_size": 4096,
        "state": state,
        "checkpoint_bytes": 0,
        "error": None,
    }
    data.update(overrides)
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_prune_manifests_age_boundary(tmp_path, monkeypatch):
    """Terminal-state manifests are deleted at exactly max_age_days old
    (inclusive); anything newer survives."""
    now = 1_800_000_000.0
    day = 86400
    monkeypatch.setattr(jobs_mod.time, "time", lambda: now)
    directory = tmp_path / "jobs"
    directory.mkdir()
    boundary = _write_manifest(
        directory, "boundary.json", "written", mtime=now - 30 * day
    )
    stale = _write_manifest(
        directory, "stale.json", "failed", mtime=now - 30 * day - 1
    )
    inside = _write_manifest(
        directory, "inside.json", "written", mtime=now - 30 * day + 1
    )

    removed = prune_manifests(directory, max_age_days=30)

    assert removed == 2
    assert not boundary.exists()
    assert not stale.exists()
    assert inside.exists()


def test_prune_manifests_only_touches_terminal_states(tmp_path):
    directory = tmp_path / "jobs"
    directory.mkdir()
    old = time.time() - 40 * 86400
    terminal = ("written", "failed", "passed", "cancelled")
    active = ("writing", "resumable", "queued")
    for state in (*terminal, *active):
        _write_manifest(directory, f"{state}.json", state, mtime=old)

    removed = prune_manifests(directory, max_age_days=30)

    # J1: passed (what CampaignRunner writes after the writer unlinked the
    # manifest) and cancelled were terminal but not prunable, so campaign
    # and cancelled-job manifests accumulated forever.
    assert removed == len(terminal)
    for state in terminal:
        assert not (directory / f"{state}.json").exists()
    for state in active:
        assert (directory / f"{state}.json").exists()


def test_prune_manifests_keeps_fresh_files(tmp_path):
    directory = tmp_path / "jobs"
    directory.mkdir()
    fresh = _write_manifest(
        directory, "fresh.json", "written", mtime=time.time()
    )

    removed = prune_manifests(directory, max_age_days=30)

    assert removed == 0
    assert fresh.exists()


def test_prune_manifests_skips_corrupt_and_foreign_files(tmp_path):
    directory = tmp_path / "jobs"
    directory.mkdir()
    old = time.time() - 40 * 86400
    corrupt = directory / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    os.utime(corrupt, (old, old))
    not_object = directory / "list.json"
    not_object.write_text("[]", encoding="utf-8")
    os.utime(not_object, (old, old))
    no_state = directory / "notes.json"
    no_state.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    os.utime(no_state, (old, old))

    removed = prune_manifests(directory, max_age_days=30)

    assert removed == 0
    assert corrupt.exists()
    assert not_object.exists()
    assert no_state.exists()


def test_prune_manifests_missing_directory_returns_zero(tmp_path):
    assert prune_manifests(tmp_path / "does-not-exist") == 0


def test_prune_manifests_deletes_saved_manifest(tmp_path):
    _, manifest = _manifest(tmp_path)
    manifest.state = "written"
    directory = tmp_path / "jobs"
    path = directory / "job.json"
    save_manifest(path, manifest)
    old = time.time() - 45 * 86400
    os.utime(path, (old, old))

    removed = prune_manifests(directory, max_age_days=30)

    assert removed == 1
    assert not path.exists()
    assert not list(directory.glob("*.tmp"))