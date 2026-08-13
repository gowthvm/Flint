"""File-copy flashing support: diskpart + format.com + robocopy helpers.

Partitioning and formatting are done with Windows built-in tools launched
through subprocess so the exact commands are visible and unit-testable.
File-copy mode requires elevation (Flint already restarts elevated via UAC).

The module is Windows-only; every entry point raises NotImplementedError on
other platforms with a helpful message.
"""

import os
import re
import subprocess
import tempfile

from core.iso import is_hybrid_iso

SCHEMES = ("auto", "gpt", "mbr")
TARGET_SYSTEMS = ("auto", "uefi", "legacy")
FILESYSTEMS = ("fat32", "ntfs", "exfat")
WRITE_MODES = ("auto", "dd", "filecopy")

_SYSTEM32 = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32"
)
_DISKPART = os.path.join(_SYSTEM32, "diskpart.exe")
_FORMAT = os.path.join(_SYSTEM32, "format.com")
_POWERSHELL = os.path.join(
    _SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe"
)


def _require_windows() -> None:
    if os.name != "nt":
        raise NotImplementedError(
            "File-copy mode is only supported on Windows; "
            "use raw (DD) mode on other platforms"
        )


def resolve_partition_scheme(partition_scheme: str, target_system: str) -> str:
    """Resolve auto choices: GPT for UEFI/auto targets, MBR for Legacy."""
    scheme = (partition_scheme or "auto").lower()
    if scheme in ("gpt", "mbr"):
        return scheme
    if (target_system or "auto").lower() == "legacy":
        return "mbr"
    return "gpt"


def resolve_write_mode(write_mode: str, iso_path: str) -> str:
    """Decide the effective write mode.

    Raw (DD) is the default and the only safe mode for hybrid ISOs, whose MBR
    boot record would be lost by a file-by-file copy.
    """
    mode = (write_mode or "auto").lower()
    if mode != "filecopy":
        return "dd"
    if is_hybrid_iso(iso_path):
        return "dd"
    return "filecopy"


def drive_number_from_path(drive_path: str) -> int:
    """Extract the disk number from ``\\\\.\\PHYSICALDRIVE<N>``."""
    match = re.search(r"PHYSICALDRIVE(\d+)$", drive_path or "", re.IGNORECASE)
    if not match:
        raise ValueError(
            f"cannot resolve drive number from {drive_path!r}"
        )
    return int(match.group(1))


def build_diskpart_script(drive_number: int, partition_scheme: str) -> str:
    """diskpart script that wipes the disk and makes one primary partition."""
    scheme = resolve_partition_scheme(partition_scheme, "auto")
    lines = [
        f"select disk {int(drive_number)}",
        "clean",
        f"convert {scheme}",
        "create partition primary",
        "assign",
    ]
    return "\n".join(lines) + "\n"


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(
            " ".join(args) + " failed" + (f": {detail}" if detail else "")
        )
    return result


def run_format(letter: str, filesystem: str) -> None:
    """Quick-format a drive letter with the given filesystem."""
    _require_windows()
    fs = (filesystem or "fat32").lower()
    if fs not in FILESYSTEMS:
        raise ValueError(f"unsupported filesystem: {filesystem}")
    _run([_FORMAT, f"{letter}:", f"/FS:{fs.upper()}", "/Q", "/Y"])


def resolve_drive_letter(drive_number: int) -> str:
    """Return the first drive letter on the disk (as assigned by diskpart)."""
    _require_windows()
    script = (
        f"(Get-Partition -DiskNumber {int(drive_number)} | "
        "Get-Volume).DriveLetter"
    )
    result = _run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script]
    )
    letter = (result.stdout or "").strip()
    if not letter:
        raise OSError("could not determine the new partition's drive letter")
    return letter[0]


def prepare_partition(
    drive_number: int, partition_scheme: str, filesystem: str
) -> str:
    """Partition + format a raw disk; returns the new partition's letter."""
    _require_windows()
    scheme = resolve_partition_scheme(partition_scheme, "auto")
    script = build_diskpart_script(drive_number, scheme)
    fd, script_path = tempfile.mkstemp(prefix="flint-diskpart-", suffix=".txt")
    # The script file is intentionally left in %TEMP% for inspection; the OS
    # cleans it up eventually.
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(script)
    _run([_DISKPART, "/s", script_path])
    letter = resolve_drive_letter(drive_number)
    run_format(letter, filesystem)
    return letter


def mount_iso(iso_path: str) -> str:
    """Mount an ISO image and return its drive letter."""
    _require_windows()
    script = (
        f"(Mount-DiskImage -ImagePath {_ps_quote(iso_path)} -PassThru | "
        "Get-Volume).DriveLetter"
    )
    result = _run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script]
    )
    letter = (result.stdout or "").strip()
    if not letter:
        raise OSError("could not mount the ISO image")
    return letter[0]


def dismount_iso(iso_path: str) -> None:
    """Unmount a mounted ISO image."""
    _require_windows()
    _run(
        [
            _POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Dismount-DiskImage -ImagePath {_ps_quote(iso_path)}",
        ]
    )


def copy_tree(source_letter: str, target_letter: str) -> None:
    """Copy a mounted ISO's contents to a drive letter with robocopy."""
    _require_windows()
    result = subprocess.run(
        [
            "robocopy",
            f"{source_letter}:\\",
            f"{target_letter}:\\",
            "/E",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
            "/R:1",
            "/W:1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # robocopy exits with 0-7 on success (>= 8 means real errors).
    if result.returncode >= 8:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(
            "robocopy failed copying files onto the drive"
            + (f": {detail}" if detail else "")
        )


def copy_iso_files(iso_path: str, target_letter: str) -> None:
    """Mount the ISO, copy its contents onto the drive, then unmount."""
    _require_windows()
    source_letter = mount_iso(iso_path)
    try:
        copy_tree(source_letter, target_letter)
    finally:
        dismount_iso(iso_path)
