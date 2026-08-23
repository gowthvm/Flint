"""Transparent decompression for compressed image files (.zip, .gz, .xz, .zst)."""

import gzip
import lzma
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

_MAGIC = {
    b"PK\x03\x04": ".zip",
    b"\x1f\x8b": ".gz",
    b"\xfd7zXZ": ".xz",
    b"\x28\xb5\x2f\xfd": ".zst",
}

COMPRESSED_EXTENSIONS = {".zip", ".gz", ".xz", ".zst"}


def _detect_by_magic(path: str) -> str | None:
    """Detect compression format by magic bytes. Returns extension or None."""
    try:
        with open(path, "rb") as f:
            header = f.read(6)
    except OSError:
        return None
    for magic, ext in _MAGIC.items():
        if header.startswith(magic):
            return ext
    return None


def is_compressed(path: str) -> bool:
    """Return True if the file appears to be a compressed image."""
    ext = os.path.splitext(path)[1].lower()
    if ext in COMPRESSED_EXTENSIONS:
        return True
    return _detect_by_magic(path) is not None


def compressed_format(path: str) -> str | None:
    """Return the detected compression format extension, or None."""
    ext = os.path.splitext(path)[1].lower()
    if ext in COMPRESSED_EXTENSIONS:
        return ext
    return _detect_by_magic(path)


@contextmanager
def decompress_image(path: str) -> Iterator[str]:
    """Context manager that yields a file path ready for raw writing.

    For non-compressed files, yields the original path.
    For compressed files, decompresses to a temp file and cleans up on exit.
    """
    if not is_compressed(path):
        yield path
        return

    fmt = compressed_format(path)
    if fmt is None:
        yield path
        return

    tmp_dir = tempfile.mkdtemp(prefix="flint-decompress-")
    try:
        if fmt == ".zip":
            extracted = _decompress_zip(path, tmp_dir)
        elif fmt == ".gz":
            extracted = _decompress_gz(path, tmp_dir)
        elif fmt == ".xz":
            extracted = _decompress_xz(path, tmp_dir)
        elif fmt == ".zst":
            extracted = _decompress_zst(path, tmp_dir)
        else:
            yield path
            return
        yield extracted
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _decompress_zip(zip_path: str, tmp_dir: str) -> str:
    """Extract the first large file from a zip archive."""
    import zipfile

    with zipfile.ZipFile(zip_path, "r") as zf:
        candidates = [
            info for info in zf.infolist()
            if not info.is_dir() and info.file_size > 0
        ]
        if not candidates:
            raise ValueError(f"zip archive {zip_path} contains no files")
        candidates.sort(key=lambda info: info.file_size, reverse=True)
        target = candidates[0]
        extracted = os.path.join(tmp_dir, os.path.basename(target.filename))
        with zf.open(target) as src, open(extracted, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return extracted


def _decompress_gz(gz_path: str, tmp_dir: str) -> str:
    """Decompress a gzip file."""
    base = os.path.splitext(os.path.basename(gz_path))[0]
    extracted = os.path.join(tmp_dir, base)
    with gzip.open(gz_path, "rb") as src, open(extracted, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return extracted


def _decompress_xz(xz_path: str, tmp_dir: str) -> str:
    """Decompress an xz file."""
    base = os.path.splitext(os.path.basename(xz_path))[0]
    extracted = os.path.join(tmp_dir, base)
    with lzma.open(xz_path, "rb") as src, open(extracted, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return extracted


def _decompress_zst(zst_path: str, tmp_dir: str) -> str:
    """Decompress a zstd file using the zstandard library."""
    try:
        import zstandard as zstd
    except ImportError:
        raise ImportError(
            "zstandard is required for .zst files: pip install zstandard"
        )
    base = os.path.splitext(os.path.basename(zst_path))[0]
    extracted = os.path.join(tmp_dir, base)
    dctx = zstd.ZstdDecompressor()
    with open(zst_path, "rb") as src, open(extracted, "wb") as dst:
        dctx.copyobj(src, dst)
    return extracted
