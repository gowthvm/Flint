"""Best-effort Linux persistence support (file-copy mode only).

Persistence lets a live Linux USB keep changes across reboots. A real
``casper-rw`` overlay needs an ext4 filesystem, which on Windows is only
available through WSL or a native ``mke2fs``/``makefs`` binary, so this
module degrades gracefully:

1. Boot configs (``grub.cfg`` / ``isolinux.cfg`` / ``syslinux.cfg``) are
   patched to pass the correct kernel parameter.
2. The backing store is created:
   - Ubuntu/Casper: a ``casper-rw`` ext4 image at the partition root.
   - Debian-style live: a ``live/persistence.conf`` overlay (works on FAT).
   The ext4 image is formatted with ``wsl mke2fs`` or ``mke2fs``/``makefs``
   when available; otherwise a sparse file is written and an informative
   warning returned.
"""

import os
import shutil
import subprocess
from collections.abc import Iterator

_GRUB_CONFIGS = (
    "grub/grub.cfg",
    "boot/grub/grub.cfg",
    "grub.cfg",
    "isolinux/isolinux.cfg",
    "isolinux.cfg",
    "syslinux/syslinux.cfg",
    "syslinux.cfg",
    "extlinux/extlinux.conf",
)

_KEYWORDS = {"casper": "persistent", "live": "persistence"}


def persistence_style(paths: set[str]) -> str:
    """'casper' for Ubuntu-style images, 'live' for Debian live, else 'casper'."""
    joined = "\n".join(sorted(paths))
    if "live/" in joined and "casper/" not in joined:
        return "live"
    if "casper/" in joined or "filesystem.squashfs" in joined:
        return "casper"
    return "casper"


def _iter_configs(root: str) -> Iterator[str]:
    for rel in _GRUB_CONFIGS:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            yield path


def _patch_grub(text: str, keyword: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if keyword in line:
            lines.append(line)
            continue
        lowered = line.lower()
        if "vmlinuz" in lowered and ("linux" in lowered or "kernel" in lowered):
            lines.append(line.rstrip("\n") + " " + keyword + "\n")
        else:
            lines.append(line)
    return "".join(lines)


def _patch_syslinux(text: str, keyword: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if keyword in line:
            lines.append(line)
            continue
        if line.lstrip().lower().startswith("append"):
            lines.append(line.rstrip("\n") + " " + keyword + "\n")
        else:
            lines.append(line)
    return "".join(lines)


def _patch_boot_configs(root: str, keyword: str) -> int:
    patched = 0
    for path in _iter_configs(root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if path.endswith((".cfg", ".conf")):
            base = os.path.basename(path).lower()
            if base == "grub.cfg":
                new_text = _patch_grub(text, keyword)
            else:
                new_text = _patch_syslinux(text, keyword)
        else:
            new_text = text
        if new_text != text:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_text)
                patched += 1
            except OSError:
                continue
    return patched


def _mke2fs_candidates() -> list[list[str]]:
    """Every ext4-formatting tool we could try, most preferred first."""
    tools: list[list[str]] = []
    if shutil.which("wsl.exe"):
        tools.append(["wsl.exe", "mke2fs"])
    for exe in ("mke2fs.exe", "makefs.exe", "mke2fs"):
        found = shutil.which(exe)
        if found:
            tools.append([found])
    return tools


def _tool_mke2fs() -> list[str] | None:
    """The preferred ext4-formatting tool, or None when none is available."""
    candidates = _mke2fs_candidates()
    return candidates[0] if candidates else None


def _wsl_path(win_path: str) -> str:
    drive, rest = win_path.replace("/", "\\").split(":", 1)
    return "/mnt/" + drive.lower() + rest.replace("\\", "/")


def _format_ext4_image(image_path: str, tool: list[str]) -> None:
    if tool[0].lower().endswith("wsl.exe"):
        command = tool + ["-t", "ext4", "-q", "-F", _wsl_path(image_path)]
    else:
        command = tool + ["-t", "ext4", "-q", "-F", image_path]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(detail or "mke2fs failed")


def create_persistence(
    root: str, size_mb: int, paths: set[str]
) -> tuple[bool, str]:
    """Create persistence on a file-copy target drive.

    ``root`` is the target drive root (e.g. ``E:\\``). Returns (ok, message).
    ``ok`` is False when persistence was only partially set up (e.g. the
    ext4 image could not be formatted).
    """
    style = persistence_style(paths)
    keyword = _KEYWORDS[style]
    notes: list[str] = []

    patched = _patch_boot_configs(root, keyword)
    notes.append(
        f"boot config{'s' if patched != 1 else ''} updated: {patched}"
        if patched
        else "no boot config found to patch"
    )

    if style == "live":
        try:
            live_dir = os.path.join(root, "live")
            os.makedirs(live_dir, exist_ok=True)
            with open(os.path.join(live_dir, "persistence.conf"), "w") as f:
                f.write("/ union\n")
        except OSError as exc:
            return False, f"could not create live/persistence.conf: {exc}"
        notes.append("live/persistence.conf overlay created")
        return True, "; ".join(notes)

    target = os.path.join(root, "casper-rw")
    size_bytes = max(64, int(size_mb)) * 1024 * 1024
    try:
        with open(target, "wb") as f:
            f.truncate(size_bytes)
    except OSError as exc:
        return False, f"could not create persistence file: {exc}"
    notes.append(f"casper-rw image created ({size_mb} MB)")

    candidates = _mke2fs_candidates()
    if not candidates:
        return (
            False,
            "; ".join(notes)
            + ". casper-rw is NOT formatted: no mke2fs/makefs or WSL found. "
            "Format it as ext4 (label casper-rw) or install WSL, or "
            "persistence will not work.",
        )
    errors: list[str] = []
    for tool in candidates:
        try:
            _format_ext4_image(target, tool)
        except OSError as exc:
            errors.append(f"{tool[0]}: {exc}")
            continue
        notes.append("formatted as ext4")
        return True, "; ".join(notes)
    return (
        False,
        "; ".join(notes)
        + f". mke2fs failed: {'; '.join(errors)}; casper-rw is unformatted.",
    )
