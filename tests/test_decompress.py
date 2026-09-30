"""Tests for compressed image support."""

import gzip
import lzma
import os
import tempfile
from types import SimpleNamespace

import pytest

from core import decompress as dc
from core.decompress import (
    compressed_format,
    decompress_image,
    is_compressed,
)


def _write_fake_image(path: str, content: bytes = b"\x00" * 1024) -> None:
    with open(path, "wb") as f:
        f.write(content)


def test_is_compressed_by_extension():
    assert is_compressed("test.zip")
    assert is_compressed("test.gz")
    assert is_compressed("test.xz")
    assert is_compressed("test.zst")
    assert not is_compressed("test.iso")
    assert not is_compressed("test.img")


def test_is_compressed_by_magic():
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as tmp:
            tmp.write(b"\x1f\x8b\x08\x00rest")
            path = tmp.name
        assert is_compressed(path)
    finally:
        if path:
            os.unlink(path)


def test_detect_format():
    assert compressed_format("test.zip") == ".zip"
    assert compressed_format("test.gz") == ".gz"
    assert compressed_format("test.iso") is None


def test_decompress_non_compressed():
    with tempfile.NamedTemporaryFile(suffix=".iso", delete=False) as tmp:
        tmp.write(b"\x00" * 100)
        tmp.flush()
        path = tmp.name
    try:
        with decompress_image(path) as result:
            assert result == path
    finally:
        os.unlink(path)


def test_decompress_gz():
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
            inner = b"fake image content " * 100
            with gzip.open(tmp, "wb") as gz:
                gz.write(inner)
            path = tmp.name
        with decompress_image(path) as result:
            assert os.path.isfile(result)
            with open(result, "rb") as f:
                assert f.read() == inner
    finally:
        if path:
            os.unlink(path)


def test_decompress_zip():
    import zipfile

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        path = tmp.name
    try:
        inner = b"zip image content " * 100
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("image.iso", inner)
        with decompress_image(path) as result:
            assert os.path.isfile(result)
            with open(result, "rb") as f:
                assert f.read() == inner
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# expansion-size guard (gz/xz/zst have no declared size)
# ---------------------------------------------------------------------------


def _gz_file(suffix: str, content: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        with gzip.open(tmp, "wb") as gz:
            gz.write(content)
        return tmp.name


def _xz_file(content: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".xz", delete=False) as tmp:
        tmp.write(lzma.compress(content))
        return tmp.name


def _fake_free(monkeypatch, free: int) -> None:
    monkeypatch.setattr(
        dc.shutil, "disk_usage", lambda p: SimpleNamespace(free=free)
    )


def test_gz_refuses_to_expand_without_free_space(monkeypatch):
    path = _gz_file(".gz", b"a" * 20_000)
    try:
        _fake_free(monkeypatch, 0)
        with pytest.raises(OSError, match="free space to expand"), decompress_image(path):
            pass
    finally:
        os.unlink(path)


def test_gz_uses_isize_hint_as_the_requirement(monkeypatch):
    """The gzip footer size is used, so the raw ratio is not demanded."""
    path = _gz_file(".gz", b"a" * 20_000)
    try:
        # 10x the ~60-byte archive is well under 10,000 B; the 20,000 B
        # hint is what makes this fail.
        _fake_free(monkeypatch, 10_000)
        with pytest.raises(OSError) as excinfo, decompress_image(path):
            pass
        assert "20,000 B" in str(excinfo.value)
    finally:
        os.unlink(path)


def test_gz_expands_when_the_hint_fits(monkeypatch):
    path = _gz_file(".gz", b"a" * 20_000)
    try:
        _fake_free(monkeypatch, 30_000)
        with decompress_image(path) as result, open(result, "rb") as f:
            assert f.read() == b"a" * 20_000
    finally:
        os.unlink(path)


def test_gz_falls_back_to_ratio_when_isize_wraps(monkeypatch):
    """Incompressible payloads make ISIZE smaller than the archive, which
    is what a wrapped 4 GiB counter looks like: the ratio is used."""
    path = _gz_file(".gz", os.urandom(2_000))
    try:
        required = dc._required_expanded_size(path, ".gz")
        compressed = os.path.getsize(path)
        assert required == compressed * dc._EXPANSION_RATIO
        _fake_free(monkeypatch, 5_000)
        with pytest.raises(OSError, match="free space to expand"), decompress_image(path):
            pass
    finally:
        os.unlink(path)


def test_xz_uses_ratio_budget(monkeypatch):
    path = _xz_file(b"\x00" * 100_000)
    try:
        compressed = os.path.getsize(path)
        assert compressed < 5_000  # highly compressible, tiny archive
        _fake_free(monkeypatch, compressed * dc._EXPANSION_RATIO - 1)
        with pytest.raises(OSError, match="free space to expand"), decompress_image(path):
            pass
    finally:
        os.unlink(path)


def test_space_check_skipped_when_disk_usage_unavailable(monkeypatch):
    def _boom(p):
        raise OSError("no volume")

    monkeypatch.setattr(dc.shutil, "disk_usage", _boom)
    path = _gz_file(".gz", b"hello world" * 10)
    try:
        with decompress_image(path) as result, open(result, "rb") as f:
            assert f.read() == b"hello world" * 10
    finally:
        os.unlink(path)


def test_required_size_prefers_usable_gzip_hint(tmp_path):
    compressible = tmp_path / "big.gz"
    compressible.write_bytes(gzip.compress(b"b" * 50_000))
    expected = dc._required_expanded_size(str(compressible), ".gz")
    assert expected == 50_000
    assert expected > compressible.stat().st_size
