from core.bootcheck import parse_boot_headers

_ESP_GUID = bytes.fromhex("28732ac1f8f1d211ba4b00a0c93ec93b")


def _mbr(signature=True, partition=False):
    data = bytearray(1024)
    if signature:
        data[510:512] = b"\x55\xaa"
    if partition:
        data[446] = 0x80  # boot indicator: active
        data[450] = 0x07  # partition type: NTFS/exFAT
        data[454:458] = (2048).to_bytes(4, "little")  # start LBA
        data[458:462] = (4096).to_bytes(4, "little")  # sector count
    return data


def _partition(
    data,
    index=0,
    boot=0x80,
    ptype=0x07,
    start_lba=2048,
    sectors=4096,
):
    base = 446 + index * 16
    data[base] = boot
    data[base + 4] = ptype
    data[base + 8 : base + 12] = start_lba.to_bytes(4, "little")
    data[base + 12 : base + 16] = sectors.to_bytes(4, "little")
    return data


def _gpt_header(data, entry_count=1, entry_size=128, entries_lba=2):
    data[512:520] = b"EFI PART"
    data[512 + 72 : 512 + 80] = entries_lba.to_bytes(8, "little")
    data[512 + 80 : 512 + 84] = entry_count.to_bytes(4, "little")
    data[512 + 84 : 512 + 88] = entry_size.to_bytes(4, "little")
    return data


def test_parse_reports_valid_mbr_layout():
    result = parse_boot_headers(bytes(_mbr()))

    assert result["status"] == "valid"
    assert result["mbr_signature"] is True
    assert result["gpt"] is False


def test_parse_reports_gpt_layout():
    data = _mbr()
    data[512:520] = b"EFI PART"

    result = parse_boot_headers(bytes(data))

    assert result["status"] == "valid"
    assert result["gpt"] is True


def test_parse_reports_protective_mbr_partition():
    data = _mbr(signature=True)
    _partition(data, boot=0x00, ptype=0xEE, start_lba=1, sectors=0xFFFFFFFF)
    data[512:520] = b"EFI PART"

    result = parse_boot_headers(bytes(data))

    assert result["status"] == "valid"
    assert result["gpt"] is True
    # 0xEE (protective) is not an ESP and the entry is not active.
    assert result["efi_partition"] is False


def test_parse_reports_bootable_mbr_partition():
    result = parse_boot_headers(bytes(_mbr(partition=True)))

    assert result["status"] == "valid"
    assert result["efi_partition"] is True


def test_garbage_boot_indicator_is_not_a_boot_layout():
    """C04: the boot byte must be 0x00 or 0x80.  Random bytes in the
    partition table are corruption, not evidence of a bootable drive."""
    data = _mbr(signature=True)
    _partition(data, boot=0x5A, ptype=0x07)

    result = parse_boot_headers(bytes(data))

    assert result["efi_partition"] is False


def test_zero_length_partition_is_not_a_boot_layout():
    data = _mbr(signature=True)
    _partition(data, boot=0x80, ptype=0x07, start_lba=0, sectors=0)

    result = parse_boot_headers(bytes(data))

    assert result["efi_partition"] is False


def test_untyped_partition_is_not_a_boot_layout():
    data = _mbr(signature=True)
    _partition(data, boot=0x80, ptype=0x00)

    result = parse_boot_headers(bytes(data))

    assert result["efi_partition"] is False


def test_fat32_lba_partition_counts_without_active_flag():
    data = _mbr(signature=True)
    _partition(data, boot=0x00, ptype=0x0C)

    result = parse_boot_headers(bytes(data))

    assert result["efi_partition"] is True


def test_efi_system_partition_type_counts_on_mbr():
    data = _mbr(signature=True)
    _partition(data, boot=0x00, ptype=0xEF)

    result = parse_boot_headers(bytes(data))

    assert result["efi_partition"] is True


def test_garbage_headers_never_report_a_bootable_layout():
    """Deterministic garbage: no 0x55AA, no GPT signature, partition
    entries full of random bytes.  Nothing here may be reported valid."""
    data = bytes(range(256)) * 4

    result = parse_boot_headers(data)

    assert result["status"] == "warning"
    assert result["mbr_signature"] is False
    assert result["gpt"] is False
    assert result["efi_partition"] is False
    assert result["error"] is None


def test_parse_reports_gpt_efi_system_partition():
    data = bytearray(4096)
    data[510:512] = b"\x55\xaa"
    _gpt_header(data, entry_count=4)
    data[1024 : 1024 + 16] = _ESP_GUID

    result = parse_boot_headers(bytes(data))

    assert result["gpt"] is True
    assert result["efi_partition"] is True


def test_gpt_esp_is_read_from_the_lba_the_header_names():
    data = bytearray(4096)
    data[510:512] = b"\x55\xaa"
    _gpt_header(data, entry_count=4, entries_lba=3)
    data[1536 + 128 : 1536 + 144] = _ESP_GUID

    result = parse_boot_headers(bytes(data))

    assert result["gpt"] is True
    assert result["efi_partition"] is True


def test_gpt_esp_is_not_scanned_inside_the_header():
    """B32: the entry array is wherever the header's "partition entries
    starting LBA" field points (LBA 2 by convention), not 92 bytes into
    the header itself. A GUID parked in the header's reserved field is
    not an ESP."""
    data = bytearray(4096)
    data[510:512] = b"\x55\xaa"
    _gpt_header(data, entry_count=4)
    data[512 + 92 : 512 + 108] = _ESP_GUID

    result = parse_boot_headers(bytes(data))

    assert result["gpt"] is True
    assert result["efi_partition"] is False


def test_parse_reports_warning_for_unmarked_header():
    result = parse_boot_headers(bytes(_mbr(signature=False)))

    assert result["status"] == "warning"
    assert result["error"] is None


def test_parse_reports_failure_for_short_read():
    result = parse_boot_headers(b"short")

    assert result == {
        "status": "failed",
        "mbr_signature": False,
        "gpt": False,
        "efi_partition": False,
        "error": "could not read drive header",
    }