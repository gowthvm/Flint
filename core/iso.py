"""ISO image inspection helpers (hybrid detection, etc.)."""

_SECTOR = 512
_PVD_SECTOR_OFFSET = 32769  # ISO9660 primary volume descriptor (2048*16 + 1)
_ISO9660_MARKER = b"CD001"
_MBR_PARTITION_START = 446
_MBR_PARTITION_END = 510
_MBR_SIGNATURE_START = 510
_MBR_SIGNATURE_END = 512
_HEAD_SIZE = 2048 * 17  # covers the PVD plus the MBR region at offset 0


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
