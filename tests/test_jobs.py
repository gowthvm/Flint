import json

import pytest

from core.jobs import JobManifest, load_manifest, save_manifest, source_sha256


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