from core.bootcheck import parse_boot_headers


def _mbr(signature=True, partition=False):
    data = bytearray(1024)
    if signature:
        data[510:512] = b"\x55\xaa"
    if partition:
        data[446] = 0x80
        data[450] = 0x07
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
    data[446] = 0x00
    data[450] = 0xEE
    data[512:520] = b"EFI PART"

    result = parse_boot_headers(bytes(data))

    assert result["status"] == "valid"
    assert result["gpt"] is True
    assert result["efi_partition"] is False


def test_parse_reports_bootable_mbr_partition():
    result = parse_boot_headers(bytes(_mbr(partition=True)))

    assert result["status"] == "valid"
    assert result["efi_partition"] is True


def test_parse_reports_gpt_efi_system_partition():
    data = _mbr()
    data[512:520] = b"EFI PART"
    data[512 + 80 : 512 + 84] = (1).to_bytes(4, "little")
    data[512 + 84 : 512 + 88] = (128).to_bytes(4, "little")
    data[512 + 92 : 512 + 108] = bytes.fromhex(
        "28732ac1f8f1d211ba4b00a0c93ec93b"
    )

    result = parse_boot_headers(bytes(data))

    assert result["gpt"] is True
    assert result["efi_partition"] is True


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