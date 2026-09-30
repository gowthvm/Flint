"""File-copy flashing support: diskpart + format.com + robocopy helpers.

Partitioning and formatting are done with Windows built-in tools launched
through subprocess so the exact commands are visible and unit-testable.
File-copy mode requires elevation (Flint already restarts elevated via UAC).

The module is Windows-only; every entry point raises NotImplementedError on
other platforms with a helpful message.
"""

import locale
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable

from core.deviceio import _Cancelled
from core.iso import detect_linux_iso, is_hybrid_iso

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
_DISM = os.path.join(_SYSTEM32, "dism.exe")
_BCD_BOOT = os.path.join(_SYSTEM32, "bcdboot.exe")
_WINDOWS_IMAGE_NAMES = ("install.wim", "install.esd", "install.swm")


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
    boot record would be lost by a file-by-file copy.  Linux ISOs (Ubuntu,
    Fedora, etc.) use ISO9660/UDF which Windows cannot read natively, so
    ``auto`` mode switches to file-copy for them — matching Rufus behaviour
    and letting Windows Explorer show the drive contents.
    """
    mode = (write_mode or "auto").lower().strip()
    # D02: the CLI help and the docs use "raw" and "file-copy" as well as
    # "dd"/"filecopy"; accept both spellings so a literal value the help
    # advertises can never silently fall through to dd.
    mode = {"raw": "dd", "file-copy": "filecopy", "file copy": "filecopy"}.get(
        mode, mode
    )
    if is_hybrid_iso(iso_path):
        return "dd"
    if mode == "filecopy":
        return "filecopy"
    if mode == "auto" and detect_linux_iso(iso_path):
        return "filecopy"
    return "dd"


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
    ]
    if scheme == "mbr":
        lines.append("active")
    lines.append("assign")
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


def _popen_cancellable(
    args: list[str],
    is_cancelled: Callable[[], bool] | None = None,
    *,
    poll_seconds: float = 0.2,
    grace_seconds: float = 3.0,
) -> subprocess.CompletedProcess[str]:
    """Run a long-lived tool and kill it promptly when cancel trips (L22).

    robocopy and dism can run for minutes; a cancel must not wait for them
    to finish. The child is terminated first, then killed after a grace
    period, and ``_Cancelled`` propagates so the caller reports
    "cancelled". Output goes to temp files (not pipes) so a chatty child
    can never deadlock the poll loop. Non-zero exits are returned, not
    raised — robocopy's 0-7 exit codes are success.
    """
    encoding = locale.getpreferredencoding(False)
    with (
        tempfile.TemporaryFile() as out_f,
        tempfile.TemporaryFile() as err_f,
    ):
        proc = subprocess.Popen(args, stdout=out_f, stderr=err_f)
        while proc.poll() is None:
            if is_cancelled is not None and is_cancelled():
                proc.terminate()
                try:
                    proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=grace_seconds)
                raise _Cancelled()
            time.sleep(poll_seconds)
        out_f.seek(0)
        err_f.seek(0)
        stdout = out_f.read().decode(encoding, errors="replace")
        stderr = err_f.read().decode(encoding, errors="replace")
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _run_cancellable(
    args: list[str],
    is_cancelled: Callable[[], bool] | None = None,
    *,
    poll_seconds: float = 0.2,
    grace_seconds: float = 3.0,
) -> subprocess.CompletedProcess[str]:
    """``_run`` with cancel support: identical semantics when
    ``is_cancelled`` is None; otherwise the child is killed on cancel.

    Partitioning deliberately stays on the blocking ``_run`` — killing
    diskpart mid-script can leave the partition table half-written, so
    partitioning always runs to completion and cancel is checked between
    steps instead.
    """
    if is_cancelled is None:
        return _run(args)
    result = _popen_cancellable(
        args, is_cancelled, poll_seconds=poll_seconds, grace_seconds=grace_seconds
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


def copy_tree(
    source_letter: str,
    target_letter: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> None:
    """Copy a mounted ISO's contents to a drive letter with robocopy.

    With ``is_cancelled``, a cancel kills robocopy mid-copy and raises
    ``_Cancelled`` (L22) instead of letting it run for minutes.
    """
    _require_windows()
    args = [
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
    ]
    if is_cancelled is None:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False
        )
    else:
        result = _popen_cancellable(args, is_cancelled)
    # robocopy exits with 0-7 on success (>= 8 means real errors).
    if result.returncode >= 8:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(
            "robocopy failed copying files onto the drive"
            + (f": {detail}" if detail else "")
        )


def copy_iso_files(
    iso_path: str,
    target_letter: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> None:
    """Mount the ISO, copy its contents onto the drive, then unmount."""
    _require_windows()
    source_letter = mount_iso(iso_path)
    try:
        copy_tree(source_letter, target_letter, is_cancelled=is_cancelled)
    finally:
        dismount_iso(iso_path)


def _find_windows_image(source_letter: str) -> str | None:
    sources = f"{source_letter}:\\sources"
    for name in _WINDOWS_IMAGE_NAMES:
        candidate = os.path.join(sources, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def apply_windows_image(
    iso_path: str,
    target_letter: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> None:
    """Apply a Windows installation image onto a drive (Windows To Go).

    Mounts the ISO, applies ``sources/install.wim|esd|swm`` with ``dism`` and
    installs boot files with ``bcdboot`` (UEFI + legacy). The target
    partition must be NTFS; requires elevation. The image index defaults to 1
    (see README for multi-edition images).

    With ``is_cancelled``, a cancel kills dism mid-apply and raises
    ``_Cancelled`` (L22); ``bcdboot`` stays blocking (it takes under a
    second) and the ISO is always unmounted.
    """
    _require_windows()
    source_letter = mount_iso(iso_path)
    try:
        image = _find_windows_image(source_letter)
        if image is None:
            raise OSError(
                "no sources/install.wim, install.esd or install.swm found "
                "on the mounted image"
            )
        _run_cancellable(
            [
                _DISM,
                "/Apply-Image",
                f"/ImageFile:{image}",
                "/Index:1",
                f"/ApplyDir:{target_letter}:\\",
            ],
            is_cancelled,
        )
        _run([_BCD_BOOT, f"{target_letter}:\\Windows", "/f", "ALL"])
    finally:
        dismount_iso(iso_path)
