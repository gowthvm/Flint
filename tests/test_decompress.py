"""Tests for compressed image support."""

import gzip
import os
import tempfile

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
