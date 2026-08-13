"""Minimal ISO9660 image builder used by detection tests.

Builds a real ISO9660 bridge tree (PVD + root directory + subdirectories)
so ``core.iso`` scans the fixture the same way it scans a real image.
"""

import struct

_LOGICAL = 2048
_PVD_LBA = 16
_TERM_LBA = 17
_FIRST_LBA = 18


def _record_len(name: bytes) -> int:
    n = len(name)
    return 33 + n + (1 if n % 2 == 0 else 0)


def _dir_record(extent: int, size: int, is_dir: bool, name: bytes) -> bytes:
    length = _record_len(name)
    rec = bytearray(length)
    rec[0] = length
    rec[2:6] = struct.pack("<I", extent)
    rec[6:10] = struct.pack(">I", extent)
    rec[10:14] = struct.pack("<I", size)
    rec[14:18] = struct.pack(">I", size)
    rec[25] = 0x02 if is_dir else 0x00
    rec[28:32] = struct.pack("<I", 1)
    rec[32] = len(name)
    rec[33 : 33 + len(name)] = name
    return bytes(rec)


def _all_files(entries):
    for entry in entries:
        if entry[0] == "f":
            yield entry
        else:
            yield from _all_files(entry[2])


def build_iso(path: str, entries) -> None:
    """Write an ISO9660 image with the given tree.

    ``entries`` is a list of items:
      ("d", name, [children])  -- directory
      ("f", name, size)        -- file with ``size`` bytes of data
    """
    nodes: list[tuple[int, list, int]] = []

    def walk(entries, parent_extent: int) -> None:
        extent = _FIRST_LBA + len(nodes)
        nodes.append((extent, entries, parent_extent))
        for entry in entries:
            if entry[0] == "d":
                walk(entry[2], extent)

    walk(entries, _FIRST_LBA)
    root_extent, root_entries, _ = nodes[0]
    nodes[0] = (root_extent, root_entries, root_extent)  # root ".." -> itself

    extent_by_children = {id(children): extent for extent, children, _ in nodes}

    files_start = _FIRST_LBA + len(nodes)
    file_offsets: dict[int, int] = {}
    cursor = files_start
    for entry in _all_files(entries):
        file_offsets[id(entry)] = cursor
        cursor += max(1, (entry[2] + _LOGICAL - 1) // _LOGICAL)
    total_sectors = cursor

    def dir_size(children: list) -> int:
        size = _record_len(b".") + _record_len(b"..")
        for entry in children:
            suffix = b";1" if entry[0] == "f" else b""
            size += _record_len(entry[1].encode("ascii") + suffix)
        return size

    def dir_blob(extent: int, children: list, parent_extent: int) -> bytes:
        recs = [
            _dir_record(extent, dir_size(children), True, b"."),
            _dir_record(parent_extent, 0, True, b".."),
        ]
        for entry in children:
            if entry[0] == "d":
                child_extent = extent_by_children[id(entry[2])]
                recs.append(
                    _dir_record(
                        child_extent,
                        dir_size(entry[2]),
                        True,
                        entry[1].encode("ascii"),
                    )
                )
            else:
                recs.append(
                    _dir_record(
                        file_offsets[id(entry)],
                        entry[2],
                        False,
                        entry[1].encode("ascii") + b";1",
                    )
                )
        data = b"".join(recs)
        return data + b"\x00" * (-len(data) % _LOGICAL)

    pvd = bytearray(_LOGICAL)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = b"FLINTTEST".ljust(32, b" ")
    pvd[40:72] = b"TESTVOL".ljust(32, b" ")
    pvd[80:88] = struct.pack("<I", total_sectors) + struct.pack(">I", total_sectors)
    pvd[120:122] = struct.pack("<H", 1)  # volume set size
    pvd[122:124] = struct.pack(">H", 1)
    pvd[128:130] = struct.pack("<H", 1)  # volume sequence number
    pvd[130:132] = struct.pack(">H", 1)
    pvd[132:134] = struct.pack("<H", _LOGICAL)  # logical block size
    pvd[134:136] = struct.pack(">H", _LOGICAL)
    pvd[156:190] = _dir_record(root_extent, dir_size(root_entries), True, b".")

    terminator = bytearray(_LOGICAL)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1

    with open(path, "wb") as f:
        f.write(b"\x00" * (_PVD_LBA * _LOGICAL))
        f.write(pvd)
        f.write(terminator)
        f.writelines(
            dir_blob(extent, children, parent) for extent, children, parent in nodes
        )
        for entry in _all_files(entries):
            f.write(b"\x00" * entry[2])
            f.write(b"\x00" * (-entry[2] % _LOGICAL))


def linux_casper_tree():
    """Ubuntu-style: casper/ with filesystem.squashfs, plus boot/grub."""
    return [
        ("d", "casper", [
            ("f", "filesystem.squashfs", 2048),
            ("f", "vmlinuz", 2048),
        ]),
        ("d", "boot", [
            ("d", "grub", [("f", "grub.cfg", 2048)]),
        ]),
        ("f", "md5sum.txt", 2048),
    ]


def linux_live_tree():
    """Debian-live style: live/ with filesystem.squashfs."""
    return [
        ("d", "live", [
            ("f", "filesystem.squashfs", 2048),
        ]),
    ]


def windows_tree():
    return [
        ("d", "sources", [
            ("f", "install.wim", 2048),
            ("f", "boot.wim", 2048),
        ]),
        ("d", "efi", [
            ("d", "boot", [("f", "bootx64.efi", 2048)]),
        ]),
    ]
