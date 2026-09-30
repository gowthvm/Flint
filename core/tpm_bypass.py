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

The bypass runs *after* the image has already been copied, so every
failure is reported as :class:`TpmBypassError` (an ``OSError``) whose
message separates "copied but bypass failed" from a failed write.  The
offline ``HKLM\\OFFLINE`` hive and the WIM mount are always released on
every path that created them, so a failed attempt does not poison the
next one.
"""

import logging
import os
import subprocess
import tempfile
import time

logger = logging.getLogger("flint")

_SYSTEM32 = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32"
)
_DISM = os.path.join(_SYSTEM32, "dism.exe")
_REG = os.path.join(_SYSTEM32, "reg.exe")

_HIVE_KEY = "HKLM\\OFFLINE"
#: ``reg unload`` can fail transiently while the registry still holds the
#: hive file open; retry a couple of times before giving up.
_UNLOAD_RETRIES = 3
_UNLOAD_RETRY_DELAY = 0.5


class TpmBypassError(OSError):
    """``boot.wim`` could not be patched even though the image was copied.

    Subclass of :class:`OSError` so existing ``except OSError`` handling
    and callers keep working.

    ``cleanup_ok`` is False when the offline SYSTEM hive or the WIM mount
    could not be released; the message then carries the command needed to
    clear that state before the next attempt.
    """

    def __init__(self, message: str, *, cleanup_ok: bool = True) -> None:
        super().__init__(message)
        self.cleanup_ok = cleanup_ok


def patch_boot_wim_on_usb(drive_letter: str) -> None:
    """Inject TPM bypass keys into ``boot.wim`` on the given USB drive.

    Parameters
    ----------
    drive_letter:
        Single drive letter, e.g. ``"E"``.

    Raises
    ------
    TpmBypassError
        If ``boot.wim`` is missing or any dism / reg step fails.  The
        image is on the drive by this point, so the message states that
        only the patch is missing, and explains how to retry.
    """
    boot_wim = f"{drive_letter}:\\sources\\boot.wim"
    if not os.path.isfile(boot_wim):
        raise _bypass_error(
            TpmBypassError(
                f"boot.wim not found at {boot_wim} — not Windows Setup media?"
            ),
            drive_letter,
        )

    mount_dir = tempfile.mkdtemp(prefix="flint_wim_")
    failure: TpmBypassError | None = None
    try:
        failure = _apply_patch(boot_wim, mount_dir)
    finally:
        try:
            os.rmdir(mount_dir)
        except OSError:
            pass

    if failure is not None:
        raise _bypass_error(failure, drive_letter)


def _apply_patch(boot_wim: str, mount_dir: str) -> TpmBypassError | None:
    """Mount, patch and unmount, returning the failure instead of raising.

    The unmount runs even when the patch fails, and a cleanup problem is
    folded into the returned error instead of masking the real cause.
    """
    try:
        _mount(boot_wim, mount_dir)
    except Exception as exc:
        return TpmBypassError(
            f"DISM could not mount {boot_wim}: {_describe(exc)}"
        )

    patch_error: TpmBypassError | None = None
    try:
        _inject_registry(mount_dir)
    except TpmBypassError as exc:
        patch_error = exc
    except Exception as exc:
        patch_error = TpmBypassError(
            f"could not patch the offline SYSTEM hive: {_describe(exc)}"
        )

    try:
        _unmount(mount_dir)
    except Exception as exc:
        unmount_error = TpmBypassError(
            f"DISM could not unmount the mounted image: {_describe(exc)} — "
            f"run `dism /Unmount-Image /MountDir:{mount_dir} /Discard` "
            "(or `dism /cleanup-wim`) before retrying",
            cleanup_ok=False,
        )
        if patch_error is None:
            return unmount_error
        return TpmBypassError(
            f"{patch_error}; {unmount_error}",
            cleanup_ok=False,
        )

    return patch_error


def _bypass_error(error: TpmBypassError, drive_letter: str) -> TpmBypassError:
    """Frame a step failure as "copied, but the bypass failed"."""
    reason = str(error)
    if error.cleanup_ok:
        retry = (
            "No offline hive or WIM mount was left behind, so re-running "
            "the flash retries cleanly."
        )
    else:
        retry = (
            "Clear the leftover state named above first "
            "(`reg unload HKLM\\OFFLINE` or `dism /cleanup-wim`), then "
            "re-run the flash — otherwise the next attempt starts against "
            "a hive/mount that is still in use."
        )
    return TpmBypassError(
        f"TPM bypass failed after the image was copied to {drive_letter}: "
        f"{reason}. Only sources\\boot.wim is unpatched, so the flash is "
        f"reported as failed even though the copy completed. {retry}",
        cleanup_ok=error.cleanup_ok,
    )


def _describe(exc: BaseException) -> str:
    """Short, human-readable detail for a failed dism / reg invocation."""
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or "").strip()
        last_line = detail.splitlines()[-1] if detail else ""
        argv = (
            list(exc.cmd)
            if isinstance(exc.cmd, (list, tuple))
            else [str(exc.cmd)]
        )
        step = os.path.basename(str(argv[0])) if argv else "command"
        if len(argv) > 1:
            step = f"{step} {argv[1]}"
        message = f"{step} exited with status {exc.returncode}"
        return f"{message}: {last_line}" if last_line else message
    return str(exc) or type(exc).__name__


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


def _unload_hive(hive_key: str) -> BaseException | None:
    """Unload the offline hive, retrying briefly.

    Returns ``None`` on success or the last failure; never raises, so it
    is safe to call from a ``finally`` that is already unwinding.
    """
    last: BaseException | None = None
    for attempt in range(1, _UNLOAD_RETRIES + 1):
        logger.info(
            "reg: unloading offline hive (attempt %d/%d)",
            attempt,
            _UNLOAD_RETRIES,
        )
        try:
            subprocess.run(
                [_REG, "unload", hive_key],
                check=True,
                capture_output=True,
                text=True,
            )
            return None
        except Exception as exc:  # reported to the caller, not raised here
            last = exc
            if attempt < _UNLOAD_RETRIES:
                time.sleep(_UNLOAD_RETRY_DELAY)
    return last


def _inject_registry(mount_dir: str) -> None:
    """Load the offline SYSTEM hive and add LabConfig bypass keys.

    The hive is always unloaded again: the unload runs in a ``finally``
    covering every path that loaded it (success, a failed ``reg add``,
    even an interrupt), and is retried briefly because a leftover
    ``HKLM\\OFFLINE`` makes every later attempt fail.  When the hive
    cannot be unloaded the failure is reported with the command that
    clears it rather than silently swallowed.
    """
    hive_path = os.path.join(
        mount_dir, "Windows", "System32", "config", "SYSTEM"
    )
    if not os.path.isfile(hive_path):
        raise TpmBypassError(f"SYSTEM hive not found at {hive_path}")

    hive_loaded = False
    failure: BaseException | None = None
    unload_failure: BaseException | None = None
    try:
        logger.info("reg: loading offline SYSTEM hive")
        subprocess.run(
            [_REG, "load", _HIVE_KEY, hive_path],
            check=True,
            capture_output=True,
            text=True,
        )
        hive_loaded = True
        labconfig = f"{_HIVE_KEY}\\Setup\\LabConfig"
        for name in (
            "BypassTPMCheck",
            "BypassSecureBootCheck",
            "BypassRAMCheck",
        ):
            logger.info("reg: setting %s = 1", name)
            subprocess.run(
                [
                    _REG,
                    "add",
                    labconfig,
                    "/v",
                    name,
                    "/t",
                    "REG_DWORD",
                    "/d",
                    "1",
                    "/f",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
    except Exception as exc:  # re-raised below as a TpmBypassError
        failure = exc
    finally:
        if hive_loaded:
            unload_failure = _unload_hive(_HIVE_KEY)

    if failure is None and unload_failure is None:
        return
    raise _inject_error(failure, unload_failure)


def _inject_error(
    failure: BaseException | None,
    unload_failure: BaseException | None,
) -> TpmBypassError:
    """One actionable error covering patch and unload problems."""
    if failure is None:
        detail = (
            _describe(unload_failure)
            if unload_failure is not None
            else "unknown error"
        )
        return TpmBypassError(
            f"the LabConfig keys were written but {_HIVE_KEY} could not be "
            f"unloaded: {detail} — run "
            f"`reg unload {_HIVE_KEY}` (or reboot) so the next attempt can "
            "load the hive",
            cleanup_ok=False,
        )
    message = f"could not patch the offline SYSTEM hive: {_describe(failure)}"
    if unload_failure is None:
        return TpmBypassError(message)
    return TpmBypassError(
        f"{message}; {_HIVE_KEY} could not be unloaded either: "
        f"{_describe(unload_failure)} — run `reg unload {_HIVE_KEY}` "
        "(or reboot) before retrying",
        cleanup_ok=False,
    )
