"""Windows 11 TPM / Secure Boot / RAM check bypass.

Patches ``sources\\boot.wim`` on a prepared USB drive by injecting registry
keys into the offline SYSTEM hive so that Windows Setup skips the hardware
requirement checks.  The bypass targets the ``LabConfig`` key:

- ``BypassTPMCheck = 1``
- ``BypassSecureBootCheck = 1``
- ``BypassRAMCheck = 1``

Method: offline registry hive injection into ``boot.wim`` index 2 (the
Windows Setup / WinPE environment).  This is the same technique used by
Rufus and does not modify any Windows executables — only data (registry
entries) inside the WIM image.
"""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("flint")

_SYSTEM32 = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32"
)
_DISM = os.path.join(_SYSTEM32, "dism.exe")
_REG = os.path.join(_SYSTEM32, "reg.exe")


def patch_boot_wim_on_usb(drive_letter: str) -> None:
    """Inject TPM bypass keys into ``boot.wim`` on the given USB drive.

    Parameters
    ----------
    drive_letter:
        Single drive letter, e.g. ``"E"``.

    Raises
    ------
    OSError
        If ``boot.wim`` is missing, or any dism / reg command fails.
    """
    boot_wim = f"{drive_letter}:\\sources\\boot.wim"
    if not os.path.isfile(boot_wim):
        raise OSError(f"boot.wim not found at {boot_wim}")

    mount_dir = tempfile.mkdtemp(prefix="flint_wim_")
    try:
        _mount(boot_wim, mount_dir)
        try:
            _inject_registry(mount_dir)
        finally:
            _unmount(mount_dir)
    finally:
        try:
            os.rmdir(mount_dir)
        except OSError:
            pass


def _mount(wim_path: str, mount_dir: str) -> None:
    """Mount ``boot.wim`` index 2 (Windows Setup / WinPE)."""
    logger.info("dism: mounting %s index 2 -> %s", wim_path, mount_dir)
    subprocess.run(
        [
            _DISM,
            "/Mount-Wim",
            f"/WimFile:{wim_path}",
            "/Index:2",
            f"/MountDir:{mount_dir}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _unmount(mount_dir: str) -> None:
    """Unmount and commit changes."""
    logger.info("dism: unmounting %s", mount_dir)
    subprocess.run(
        [
            _DISM,
            "/Unmount-Image",
            f"/MountDir:{mount_dir}",
            "/Commit",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _inject_registry(mount_dir: str) -> None:
    """Load the offline SYSTEM hive and add LabConfig bypass keys."""
    hive_path = os.path.join(
        mount_dir, "Windows", "System32", "config", "SYSTEM"
    )
    if not os.path.isfile(hive_path):
        raise OSError(f"SYSTEM hive not found at {hive_path}")

    hive_key = "HKLM\\OFFLINE"
    logger.info("reg: loading offline SYSTEM hive")
    subprocess.run(
        [_REG, "load", hive_key, hive_path],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        labconfig = f"{hive_key}\\Setup\\LabConfig"
        for name in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck"):
            logger.info("reg: setting %s = 1", name)
            subprocess.run(
                [_REG, "add", labconfig, "/v", name, "/t", "REG_DWORD", "/d", "1", "/f"],
                check=True,
                capture_output=True,
                text=True,
            )
    finally:
        logger.info("reg: unloading offline hive")
        subprocess.run(
            [_REG, "unload", hive_key],
            check=True,
            capture_output=True,
            text=True,
        )
