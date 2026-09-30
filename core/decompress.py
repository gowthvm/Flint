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

#: Expansion multiplier applied to the compressed size when the format
#: gives no trustworthy expanded-size hint (xz, zstd, gzip without a
#: usable ISIZE footer).  **N = 10**: every real image archive expands by
#: far less than that (ISO payloads are already-compressed squashfs/hfs
#: blobs, so gzip/xz/zstd ratios on them are ~1.3–4x), while a hostile
#: archive that claims to be 10 MB cannot ask the temp drive for more
#: than 100 MB.  The bound is what keeps a tiny archive from filling
#: ``%TEMP%`` during streaming.
_EXPANSION_RATIO = 10


def _fmt_bytes(count: int) -> str:
    if count >= 1024**3:
        return f"{count / 1024**3:.1f} GiB"
    if count >= 1024**2:
        return f"{count / 1024**2:.1f} MiB"
    return f"{count:,} B"


def _gzip_isize(path: str) -> int | None:
    """Uncompressed size from the gzip ISIZE footer, or None.

    The footer holds the uncompressed size modulo 2**32, so it is only a
    hint: it wraps for inputs of 4 GiB and more.
    """
    try:
        size = os.path.getsize(path)
        if size < 18:  # minimal gzip stream: 10-byte header + 8-byte footer
            return None
        with open(path, "rb") as f:
            f.seek(size - 4)
            footer = f.read(4)
    except OSError:
        return None
    isize = int.from_bytes(footer, "little")
    return isize or None


def _required_expanded_size(path: str, fmt: str) -> int:
    """Bytes of temp space that expanding *path* is expected to need."""
    try:
        compressed = os.path.getsize(path)
    except OSError:
        return 0
    if fmt == ".gz":
        isize = _gzip_isize(path)
        # Trust the footer only when it is at least the input size: a
        # wrapped (>= 4 GiB) value is almost always smaller, and a valid
        # expansion never shrinks the data.
        if isize is not None and isize >= compressed:
            return isize
    return compressed * _EXPANSION_RATIO


def _ensure_temp_space(source: str, tmp_dir: str, fmt: str) -> None:
    """Refuse to expand an archive that does not fit in the temp drive.

    gz/xz/zst carry no declared size, so the guard is an estimate: the
    gzip footer when it is usable, otherwise ``_EXPANSION_RATIO`` times
    the compressed size.  Raises ``OSError`` with the numbers when the
    estimate exceeds the free space on the temp volume; an unmeasurable
    volume (``disk_usage`` failing) is not a blocker.
    """
    try:
        required = _required_expanded_size(source, fmt)
        free = int(shutil.disk_usage(tmp_dir).free)
    except (OSError, ValueError):
        return
    if required and free < required:
        raise OSError(
            f"not enough free space to expand {os.path.basename(source)}: "
            f"needs about {_fmt_bytes(required)} "
            f"({_EXPANSION_RATIO}x compressed size or the gzip size hint), "
            f"only {_fmt_bytes(free)} free in {tmp_dir}. Free up temp "
            "space or move the image to a larger drive."
        )


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

    Before anything is expanded the temp volume is checked against an
    estimate of the output size: zip uses the declared entry size (capped
    at 16 GiB), gz uses its ISIZE footer when usable and gz/xz/zst fall
    back to ``_EXPANSION_RATIO`` times the compressed size.  Insufficient
    space raises ``OSError`` instead of streaming until the drive is full.
    """
    if not is_compressed(path):
        yield path
        return

    fmt = compressed_format(path)
    if fmt is None:
        yield path
        return

    tmp_dir = tempfile.mkdtemp(prefix="flint-decompress-")
    extracted = None
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
        # Clean up the decompressed temp file, then the temp directory.
        if extracted is not None:
            try:
                os.unlink(extracted)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def _decompress_zip(zip_path: str, tmp_dir: str) -> str:
    """Extract the first large file from a zip archive."""
    import zipfile

    _MAX_EXTRACTED_SIZE = 16 * 1024 * 1024 * 1024  # 16 GiB safety limit

    with zipfile.ZipFile(zip_path, "r") as zf:
        candidates = [
            info for info in zf.infolist()
            if not info.is_dir() and info.file_size > 0
        ]
        if not candidates:
            raise ValueError(f"zip archive {zip_path} contains no files")
        candidates.sort(key=lambda info: info.file_size, reverse=True)
        target = candidates[0]
        if target.file_size > _MAX_EXTRACTED_SIZE:
            raise ValueError(
                f"zip entry {target.filename!r} is "
                f"{target.file_size / (1024**3):.1f} GiB — "
                "extraction limit is 16 GiB (possible zip bomb)"
            )
        extracted = os.path.join(tmp_dir, os.path.basename(target.filename))
        with zf.open(target) as src, open(extracted, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return extracted


def _decompress_gz(gz_path: str, tmp_dir: str) -> str:
    """Decompress a gzip file."""
    _ensure_temp_space(gz_path, tmp_dir, ".gz")
    base = os.path.splitext(os.path.basename(gz_path))[0]
    extracted = os.path.join(tmp_dir, base)
    with gzip.open(gz_path, "rb") as src, open(extracted, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return extracted


def _decompress_xz(xz_path: str, tmp_dir: str) -> str:
    """Decompress an xz file."""
    _ensure_temp_space(xz_path, tmp_dir, ".xz")
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
        raise OSError(
            "The 'zstandard' package is required for .zst files. "
            "Install it with: pip install zstandard"
        )
    _ensure_temp_space(zst_path, tmp_dir, ".zst")
    base = os.path.splitext(os.path.basename(zst_path))[0]
    extracted = os.path.join(tmp_dir, base)
    dctx = zstd.ZstdDecompressor()
    with open(zst_path, "rb") as src, open(extracted, "wb") as dst:
        dctx.copy_stream(src, dst)
    return extracted
