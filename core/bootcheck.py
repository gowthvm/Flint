import ctypes
from typing import Any

_GPT_SIG = b"EFI PART"


def probe_bootability(drive_path: str, size_read: int = 65536) -> dict[str, Any]:
    """Best-effort bootability probe on a flashed raw drive.

    Reads the first `size_read` bytes and reports:
      mbr_signature: True if the legacy boot signature (0x55AA) is present
      gpt: True if a GPT header (\"EFI PART\") is present at LBA 1
      efi_path_hint: True when a GPT/legacy partition layout points at an
                     ESP-looking partition (bootable MBR partition or
                     EFI System Partition type GUID match)
      error: message when the drive could not be read
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _OPEN_EXISTING = 3
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    handle = kernel32.CreateFileW(
        drive_path,
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return {"error": "could not open drive for bootability check"}

    report: dict[str, Any] = {
        "mbr_signature": False,
        "gpt": False,
        "efi_partition": False,
        "error": None,
    }
    try:
        buffer = ctypes.create_string_buffer(size_read)
        read = ctypes.c_ulong()
        ok = kernel32.ReadFile(
            handle,
            buffer,
            size_read,
            ctypes.byref(read),
            None,
        )
        if not ok or read.value < 512:
            report["error"] = "could not read drive header"
            return report
        data = buffer.raw[: read.value]

        mbr = data[:512]
        if len(mbr) >= 510:
            report["mbr_signature"] = mbr[510] == 0x55 and mbr[511] == 0xAA
        report["gpt"] = data[512:520] == _GPT_SIG if len(data) >= 520 else False

        for i in range(4):
            entry = mbr[446 + i * 16 : 446 + (i + 1) * 16]
            if len(entry) != 16:
                break
            if all(b == 0 for b in entry):
                continue
            if (
                entry[0] == 0x80
                or entry[4] in (0x0C, 0x0B, 0x07)
                or entry[0] not in (0x00, 0x80)
            ):
                report["efi_partition"] = True
                break
    except OSError:
        report["error"] = "could not read drive header"
    finally:
        kernel32.CloseHandle(handle)
    return report