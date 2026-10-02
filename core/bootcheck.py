import ctypes
from typing import Any

from core.deviceio import kernel32

_GPT_SIG = b"EFI PART"
_ESP_TYPE_GUID = bytes.fromhex("28732ac1f8f1d211ba4b00a0c93ec93b")


def parse_boot_headers(data: bytes) -> dict[str, Any]:
    """Inspect boot headers without opening a device.

    This reports layout evidence only; it does not claim that firmware can
    boot the device.
    """
    report: dict[str, Any] = {
        "status": "warning",
        "mbr_signature": False,
        "gpt": False,
        "efi_partition": False,
        "error": None,
    }
    if len(data) < 512:
        report["status"] = "failed"
        report["error"] = "could not read drive header"
        return report

    mbr = data[:512]
    report["mbr_signature"] = mbr[510:512] == b"\x55\xaa"
    report["gpt"] = data[512:520] == _GPT_SIG if len(data) >= 520 else False
    if report["gpt"] and len(data) >= 512 + 92:
        entry_count = int.from_bytes(data[512 + 80 : 512 + 84], "little")
        entry_size = int.from_bytes(data[512 + 84 : 512 + 88], "little")
        entries_lba = int.from_bytes(data[512 + 72 : 512 + 80], "little")
        entries_start = entries_lba * 512
        if 128 <= entry_size <= 1024 and entries_lba > 1:
            for index in range(min(entry_count, 128)):
                start = entries_start + index * entry_size
                end = start + entry_size
                if end > len(data):
                    break
                if data[start : start + 16] == _ESP_TYPE_GUID:
                    report["efi_partition"] = True
                    break
    # MBR partition table: only structurally valid entries are evidence.
    # A boot indicator outside {0x00, 0x80}, a zero type or an entry that
    # claims no sectors is noise (or corruption), never a boot layout.
    for i in range(4):
        entry = mbr[446 + i * 16 : 446 + (i + 1) * 16]
        if len(entry) != 16 or all(b == 0 for b in entry):
            continue
        boot_indicator = entry[0]
        partition_type = entry[4]
        start_lba = int.from_bytes(entry[8:12], "little")
        sectors = int.from_bytes(entry[12:16], "little")
        if boot_indicator not in (0x00, 0x80):
            continue
        if partition_type == 0 or start_lba == 0 or sectors == 0:
            continue
        # Active partition, or a FAT/NTFS/EFI type on a drive that has no
        # GPT header: both indicate an install rather than empty media.
        if boot_indicator == 0x80 or partition_type in (
            0x0B,
            0x0C,
            0x07,
            0xEF,
        ):
            report["efi_partition"] = True
            break
    if report["gpt"] or report["mbr_signature"] or report["efi_partition"]:
        report["status"] = "valid"
    return report


def probe_bootability(drive_path: str, size_read: int = 65536) -> dict[str, Any]:
    """Best-effort bootability probe on a flashed raw drive.

    Reads the first `size_read` bytes and reports:
      mbr_signature: True if the legacy boot signature (0x55AA) is present
      gpt: True if a GPT header ("EFI PART") is present at LBA 1
      efi_path_hint: True when a GPT/legacy partition layout points at an
                     ESP-looking partition (bootable MBR partition or
                     EFI System Partition type GUID match)
      error: message when the drive could not be read
    """
    k32 = kernel32()
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    handle = k32.CreateFileW(
        drive_path,
        0x80000000,  # GENERIC_READ
        0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        report = parse_boot_headers(b"")
        report["error"] = "could not open drive for bootability check"
        return report

    parsed_report: dict[str, Any]
    try:
        buffer = ctypes.create_string_buffer(size_read)
        read = ctypes.c_ulong()
        ok = k32.ReadFile(
            handle,
            buffer,
            size_read,
            ctypes.byref(read),
            None,
        )
        if not ok or read.value < 512:
            return parse_boot_headers(b"")
        data = buffer.raw[: read.value]
        parsed_report = parse_boot_headers(data)
    except OSError:
        parsed_report = parse_boot_headers(b"")
    finally:
        k32.CloseHandle(handle)
    return parsed_report