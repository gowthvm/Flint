"""ISO image inspection helpers (hybrid detection, content scanning)."""

import os
from typing import BinaryIO, TypedDict

_SECTOR = 512
_PVD_SECTOR_OFFSET = 32769  # ISO9660 primary volume descriptor (2048*16 + 1)
_ISO9660_MARKER = b"CD001"
_MBR_PARTITION_START = 446
_MBR_PARTITION_END = 510
_MBR_SIGNATURE_START = 510
_MBR_SIGNATURE_END = 512
_HEAD_SIZE = 2048 * 17  # covers the PVD plus the MBR region at offset 0

_LOGICAL_SECTOR = 2048  # ISO9660 logical sector size
_PVD_ROOT_RECORD_OFFSET = 156
_MAX_DEPTH = 10
_MAX_RECORDS = 200_000
_MAX_DIR_BYTES = 8 * 1024 * 1024
_SCAN_CHUNK = 4 * 1024 * 1024
_WINDOWS_SCAN_CAP = 256 * 1024 * 1024  # UDF file identifiers live early


def is_hybrid_iso(path: str) -> bool:
    """True when the image is a hybrid ISO (ISO9660 + bootable MBR).

    Hybrid images boot both from CD/DVD (El Torito) and from USB (BIOS reads
    their MBR partition table). They must be written raw (DD): a file-by-file
    copy would lose the boot record.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_HEAD_SIZE)
    except OSError:
        return False
    if len(head) < _HEAD_SIZE:
        return False
    if (
        head[_PVD_SECTOR_OFFSET : _PVD_SECTOR_OFFSET + 5]
        != _ISO9660_MARKER
    ):
        return False
    if (
        head[_MBR_SIGNATURE_START : _MBR_SIGNATURE_END]
        != b"\x55\xaa"
    ):
        return False
    table = head[_MBR_PARTITION_START : _MBR_PARTITION_END]
    return any(
        entry != b"\x00" * 16
        for entry in (table[i : i + 16] for i in range(0, 64, 16))
    )


# ---------------------------------------------------------------------------
# ISO9660 content scan (no third-party deps, raw-file reads only)
# ---------------------------------------------------------------------------

def _read_logical(f: BinaryIO, lba: int) -> bytes:
    f.seek(lba * _LOGICAL_SECTOR)
    return f.read(_LOGICAL_SECTOR)


class _DirRecord(TypedDict):
    length: int
    extent: int
    size: int
    is_dir: bool
    name: str


def _parse_dir_record(buf: bytes, off: int) -> _DirRecord | None:
    if off + 33 > len(buf):
        return None
    length = buf[off]
    if length == 0 or off + length > len(buf):
        return None
    return {
        "length": length,
        "extent": int.from_bytes(buf[off + 2 : off + 6], "little"),
        "size": int.from_bytes(buf[off + 10 : off + 14], "little"),
        "is_dir": bool(buf[off + 25] & 0x02),
        "name": buf[off + 33 : off + 33 + buf[off + 32]].decode(
            "ascii", "replace"
        ),
    }


def _walk_directory(
    f: BinaryIO,
    extent: int,
    size: int,
    depth: int,
    out: set[str],
    parent: str,
    parent_extent: int,
    budget: list[int],
) -> None:
    if (
        depth > _MAX_DEPTH
        or size <= 0
        or size > _MAX_DIR_BYTES
        or budget[0] <= 0
    ):
        return
    current = extent
    sectors = (size + _LOGICAL_SECTOR - 1) // _LOGICAL_SECTOR
    # +1 tolerance for a trailing continuation fragment
    for _ in range(sectors + 1):
        data = _read_logical(f, current)
        off = 0
        parent_rec: _DirRecord | None = None
        while off + 33 <= len(data):
            rec = _parse_dir_record(data, off)
            if rec is None:
                break
            name = rec["name"]
            if name == "..":
                parent_rec = rec
            elif name not in ("", ".", "\x00"):
                budget[0] -= 1
                if budget[0] <= 0:
                    return
                path = f"{parent}/{name.split(';')[0]}".lstrip("/").lower()
                out.add(path)
                if rec["is_dir"] and rec["extent"] > 0:
                    _walk_directory(
                        f,
                        rec["extent"],
                        rec["size"],
                        depth + 1,
                        out,
                        path,
                        current,
                        budget,
                    )
            off += rec["length"]
        # A '..' record whose extent is not the parent means the directory
        # continues into the next fragment at that extent (multi-extent).
        if parent_rec is None or parent_rec["extent"] <= 0:
            return
        if parent_rec["extent"] == parent_extent:
            return
        current = parent_rec["extent"]


def list_iso_paths(path: str) -> set[str]:
    """List lowercase ISO9660 paths of the image (directories and files).

    Returns an empty set when the image has no ISO9660 bridge (e.g. pure
    UDF). Names are normalised to lowercase without version suffixes.
    """
    out: set[str] = set()
    try:
        with open(path, "rb") as f:
            pvd = _read_logical(f, 16)
            if pvd[1:6] != _ISO9660_MARKER:
                return out
            root = _parse_dir_record(pvd, _PVD_ROOT_RECORD_OFFSET)
            if root is None:
                return out
            _walk_directory(
                f,
                root["extent"],
                root["size"],
                0,
                out,
                "",
                root["extent"],
                [_MAX_RECORDS],
            )
    except OSError:
        return out
    return out


def _scan_raw(path: str, needles: list[bytes]) -> bool:
    """Scan the first chunked region of the file for byte needles."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    limit = min(size, _WINDOWS_SCAN_CAP)
    try:
        with open(path, "rb") as f:
            read = 0
            while read < limit:
                chunk = f.read(_SCAN_CHUNK)
                if not chunk:
                    break
                for needle in needles:
                    if needle in chunk:
                        return True
                read += len(chunk)
    except OSError:
        return False
    return False


def _utf16(variant: str) -> bytes:
    return variant.encode("utf-16-le")


def detect_linux_iso(path: str) -> bool:
    """True when the ISO looks Linux-like (casper / filesystem.squashfs / live)."""
    paths = list_iso_paths(path)
    for p in paths:
        parts = p.split("/")
        if "casper" in parts or "live" in parts:
            return True
        if p == "filesystem.squashfs" or p.endswith("/filesystem.squashfs"):
            return True
    return False


def detect_windows_iso(path: str) -> bool:
    """True when the ISO looks like a Windows installation image.

    Checks the ISO9660 bridge tree for ``sources/install.wim|esd|swm``, then
    falls back to a raw scan of the leading region for UDF-encoded names
    (UTF-16LE), which is where large ``install.wim`` files live.
    """
    paths = list_iso_paths(path)
    for p in paths:
        if p.startswith("sources/install."):
            return True
    needles = []
    for name in ("install.wim", "install.esd", "install.swm"):
        needles.append(name.encode("ascii"))
        needles.append(_utf16(name))
    return _scan_raw(path, needles)
