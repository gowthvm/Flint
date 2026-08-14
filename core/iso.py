"""ISO image inspection helpers (hybrid detection, content scanning)."""

import os
from typing import BinaryIO, TypedDict

_SECTOR = 512
_PVD_SECTOR_OFFSET = 32769  # ISO9660 primary volume descriptor (2048*16 + 1)
_EL_TORITO_SECTOR_OFFSET = 17 * 2048  # boot record volume descriptor
_ISO9660_MARKER = b"CD001"
_EL_TORITO_STRING = b"EL TORITO"
_ISOHYBRID_STRING = b"ISOHYBRID"
_MBR_PARTITION_START = 446
_MBR_PARTITION_END = 510
_MBR_SIGNATURE_START = 510
_MBR_SIGNATURE_END = 512
# covers the MBR (0..512), the PVD (32769) and the El Torito boot record
_HEAD_SIZE = 2048 * 18

_LOGICAL_SECTOR = 2048  # ISO9660 logical sector size
_PVD_ROOT_RECORD_OFFSET = 156
_MAX_DEPTH = 10
_MAX_RECORDS = 200_000
_MAX_DIR_BYTES = 8 * 1024 * 1024
_SCAN_CHUNK = 4 * 1024 * 1024
_WINDOWS_SCAN_CAP = 256 * 1024 * 1024  # UDF file identifiers live early


def _scan_iso_head(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            head = f.read(_HEAD_SIZE)
    except OSError:
        return None
    if len(head) < _HEAD_SIZE:
        return None
    return head


def has_el_torito(path: str) -> bool:
    """True when the image carries an El Torito boot record descriptor.

    El Torito alone does not make an image hybrid: plain CD images that boot
    from optical media carry it too. It corroborates the hybrid heuristic.
    """
    head = _scan_iso_head(path)
    if head is None:
        return False
    return _EL_TORITO_STRING in head[_EL_TORITO_SECTOR_OFFSET :]


def is_hybrid_iso(path: str) -> bool:
    """True when the image is a hybrid ISO (ISO9660 + bootable MBR).

    Hybrid images boot both from CD/DVD (El Torito) and from USB (BIOS reads
    their MBR partition table). They must be written raw (DD): a file-by-file
    copy would lose the boot record.

    Detection is an in-process fast heuristic on the first 36 KiB:
    - the ISO9660 marker at the primary volume descriptor (32769),
    - the syslinux ``ISOHYBRID`` marker written into the MBR boot code
      (decisive on its own),
    - the MBR boot signature (55AA) at offset 510 together with a non-empty
      MBR partition table.
    El Torito presence alone is not decisive (plain bootable CD images have
    it too). When the image is unreadable or too small this returns False and
    callers default to raw (DD), which is safe either way.
    """
    head = _scan_iso_head(path)
    if head is None:
        return False
    if (
        head[_PVD_SECTOR_OFFSET : _PVD_SECTOR_OFFSET + 5]
        != _ISO9660_MARKER
    ):
        return False
    # The syslinux ISOHYBRID marker is decisive on its own: it only appears
    # in images produced with isohybrid and guarantees the MBR was installed
    # (some of those images carry no 0x55AA boot signature).
    if _ISOHYBRID_STRING in head[:_MBR_PARTITION_START]:
        return True
    if (
        head[_MBR_SIGNATURE_START : _MBR_SIGNATURE_END]
        != b"\x55\xaa"
    ):
        return False
    table = head[_MBR_PARTITION_START : _MBR_PARTITION_END]
    has_partition = any(
        entry != b"\x00" * 16
        for entry in (table[i : i + 16] for i in range(0, 64, 16))
    )
    return has_partition


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


def largest_iso_file_size(path: str) -> int:
    """Largest single file (bytes) in the ISO9660 tree, or -1 when the
    image has no readable ISO9660 bridge (e.g. pure UDF).

    Used as a pre-flight check for FAT32 targets, which cannot store files
    over 4 GiB.
    """
    largest = -1
    try:
        with open(path, "rb") as f:
            pvd = _read_logical(f, 16)
            if pvd[1:6] != _ISO9660_MARKER:
                return -1
            root = _parse_dir_record(pvd, _PVD_ROOT_RECORD_OFFSET)
            if root is None:
                return -1
            budget = [_MAX_RECORDS]

            def walk(
                extent: int, size: int, depth: int, parent_extent: int
            ) -> None:
                nonlocal largest
                if (
                    depth > _MAX_DEPTH
                    or size <= 0
                    or size > _MAX_DIR_BYTES
                    or budget[0] <= 0
                ):
                    return
                sectors = (size + _LOGICAL_SECTOR - 1) // _LOGICAL_SECTOR
                for _ in range(sectors + 1):
                    data = _read_logical(f, extent)
                    off = 0
                    up: int | None = None
                    while off + 33 <= len(data):
                        rec = _parse_dir_record(data, off)
                        if rec is None:
                            break
                        if rec["name"] == "..":
                            up = rec["extent"]
                        elif rec["name"] not in ("", ".", "\x00"):
                            budget[0] -= 1
                            if budget[0] <= 0:
                                return
                            if rec["is_dir"] and rec["extent"] > 0:
                                walk(
                                    rec["extent"],
                                    rec["size"],
                                    depth + 1,
                                    extent,
                                )
                            else:
                                largest = max(largest, rec["size"])
                        off += rec["length"]
                    if up is None or up <= 0:
                        return
                    if up == parent_extent:
                        return
                    extent = up

            walk(root["extent"], root["size"], 0, root["extent"])
    except OSError:
        return -1
    return largest


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
