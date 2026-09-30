"""Tests for core.tpm_bypass — Windows 11 TPM bypass."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from core import tpm_bypass


def test_patch_boot_wim_raises_when_missing(tmp_path: object) -> None:
    with pytest.raises(OSError, match="boot.wim not found"):
        tpm_bypass.patch_boot_wim_on_usb("Z")


@patch("core.tpm_bypass._unmount")
@patch("core.tpm_bypass._inject_registry")
@patch("core.tpm_bypass._mount")
@patch("core.tpm_bypass.os.path.isfile", return_value=True)
def test_patch_boot_wim_calls_dism_and_reg(
    mock_isfile: MagicMock,
    mock_mount: MagicMock,
    mock_inject: MagicMock,
    mock_unmount: MagicMock,
    tmp_path: MagicMock,
) -> None:
    tpm_bypass.patch_boot_wim_on_usb("E")

    mock_mount.assert_called_once()
    mock_inject.assert_called_once()
    mock_unmount.assert_called_once()
    # Verify the mount was called with the correct boot.wim path
    call_args = mock_mount.call_args[0]
    assert call_args[0] == "E:\\sources\\boot.wim"


@patch("core.tpm_bypass.subprocess.run")
def test_mount_calls_dism(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    tpm_bypass._mount("D:\\sources\\boot.wim", "C:\\mount")
    call_args = mock_run.call_args[0][0]
    assert "dism.exe" in call_args[0]
    assert "/Mount-Wim" in call_args
    assert "/Index:2" in call_args


@patch("core.tpm_bypass.subprocess.run")
def test_unmount_calls_dism(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    tpm_bypass._unmount("C:\\mount")
    call_args = mock_run.call_args[0][0]
    assert "/Unmount-Image" in call_args
    assert "/Commit" in call_args


@patch("core.tpm_bypass.subprocess.run")
def test_inject_registry_loads_hive_and_adds_keys(
    mock_run: MagicMock,
    tmp_path: MagicMock,
) -> None:
    mount_dir = str(tmp_path)
    win_config = os.path.join(
        mount_dir, "Windows", "System32", "config"
    )
    os.makedirs(win_config)
    with open(os.path.join(win_config, "SYSTEM"), "wb") as f:
        f.write(b"\x00" * 16)

    mock_run.return_value = MagicMock(returncode=0)
    tpm_bypass._inject_registry(mount_dir)

    calls = mock_run.call_args_list
    assert len(calls) == 5

    load_call = calls[0][0][0]
    assert "reg.exe" in load_call[0]
    assert "load" in load_call

    for i, name in enumerate(
        ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck"),
        start=1,
    ):
        add_call = calls[i][0][0]
        assert "add" in add_call
        assert name in add_call

    unload_call = calls[4][0][0]
    assert "unload" in unload_call


@patch("core.tpm_bypass.subprocess.run")
def test_inject_registry_raises_on_missing_hive(
    mock_run: MagicMock,
    tmp_path: MagicMock,
) -> None:
    mount_dir = str(tmp_path)
    os.makedirs(os.path.join(mount_dir, "Windows", "System32", "config"))
    mock_run.return_value = MagicMock(returncode=0)

    with pytest.raises(OSError, match="SYSTEM hive not found"):
        tpm_bypass._inject_registry(mount_dir)


# ---------------------------------------------------------------------------
# failure surfaces: the copy succeeded, the patch did not; retries stay clean
# ---------------------------------------------------------------------------


def _hive_tree(tmp_path) -> str:
    mount_dir = str(tmp_path)
    win_config = os.path.join(mount_dir, "Windows", "System32", "config")
    os.makedirs(win_config, exist_ok=True)
    with open(os.path.join(win_config, "SYSTEM"), "wb") as f:
        f.write(b"\x00" * 16)
    return mount_dir


def _runner(calls, fail_on):
    """subprocess.run stand-in that records argv and fails on one step."""

    def run(argv, **kwargs):
        argv = list(argv)
        calls.append(argv)
        if fail_on in argv:
            raise tpm_bypass.subprocess.CalledProcessError(
                1, argv, stderr="access is denied"
            )
        return MagicMock(returncode=0)

    return run


@patch("core.tpm_bypass.subprocess.run")
def test_inject_registry_unloads_hive_when_add_fails(
    mock_run: MagicMock,
    tmp_path: MagicMock,
) -> None:
    mount_dir = _hive_tree(tmp_path)
    calls: list[list[str]] = []
    mock_run.side_effect = _runner(calls, "add")

    with pytest.raises(tpm_bypass.TpmBypassError) as excinfo:
        tpm_bypass._inject_registry(mount_dir)

    assert excinfo.value.cleanup_ok is True
    assert "could not patch" in str(excinfo.value)
    # The hive is still unloaded even though the reg add failed.
    assert any(c[1] == "unload" for c in calls)
    assert calls[-1][1] == "unload"


@patch("core.tpm_bypass.subprocess.run")
def test_inject_registry_reports_hive_that_stays_loaded(
    mock_run: MagicMock,
    tmp_path: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_dir = _hive_tree(tmp_path)
    monkeypatch.setattr(tpm_bypass, "_UNLOAD_RETRY_DELAY", 0)
    calls: list[list[str]] = []
    mock_run.side_effect = _runner(calls, "unload")

    with pytest.raises(tpm_bypass.TpmBypassError) as excinfo:
        tpm_bypass._inject_registry(mount_dir)

    assert excinfo.value.cleanup_ok is False
    assert "could not be unloaded" in str(excinfo.value)
    assert "reg unload HKLM\\OFFLINE" in str(excinfo.value)
    # The unload is retried rather than attempted once and abandoned.
    unload_calls = [c for c in calls if c[1] == "unload"]
    assert len(unload_calls) == tpm_bypass._UNLOAD_RETRIES


@patch("core.tpm_bypass.os.path.isfile", return_value=True)
@patch("core.tpm_bypass._mount")
def test_patch_boot_wim_mount_failure_says_copy_completed(
    mock_mount: MagicMock,
    mock_isfile: MagicMock,
) -> None:
    mock_mount.side_effect = tpm_bypass.subprocess.CalledProcessError(
        1, ["dism.exe"], stderr="not a valid wim"
    )

    with pytest.raises(tpm_bypass.TpmBypassError) as excinfo:
        tpm_bypass.patch_boot_wim_on_usb("E")

    message = str(excinfo.value)
    assert "after the image was copied" in message
    assert "reported as failed even though the copy completed" in message
    assert "re-running the flash" in message


@patch("core.tpm_bypass.os.path.isfile", return_value=True)
@patch("core.tpm_bypass._unmount")
@patch("core.tpm_bypass._inject_registry")
@patch("core.tpm_bypass._mount")
def test_patch_boot_wim_unmounts_when_inject_fails(
    mock_mount: MagicMock,
    mock_inject: MagicMock,
    mock_unmount: MagicMock,
    mock_isfile: MagicMock,
) -> None:
    mock_inject.side_effect = tpm_bypass.TpmBypassError("labconfig denied")

    with pytest.raises(tpm_bypass.TpmBypassError) as excinfo:
        tpm_bypass.patch_boot_wim_on_usb("E")

    mock_unmount.assert_called_once()
    assert "labconfig denied" in str(excinfo.value)
    assert "after the image was copied" in str(excinfo.value)
    assert excinfo.value.cleanup_ok is True


@patch("core.tpm_bypass.os.path.isfile", return_value=True)
@patch("core.tpm_bypass._unmount")
@patch("core.tpm_bypass._inject_registry")
@patch("core.tpm_bypass._mount")
def test_patch_boot_wim_keeps_cause_when_unmount_fails(
    mock_mount: MagicMock,
    mock_inject: MagicMock,
    mock_unmount: MagicMock,
    mock_isfile: MagicMock,
) -> None:
    mock_inject.side_effect = tpm_bypass.TpmBypassError("labconfig denied")
    mock_unmount.side_effect = tpm_bypass.subprocess.CalledProcessError(
        1, ["dism.exe"], stderr="the image is in use"
    )

    with pytest.raises(tpm_bypass.TpmBypassError) as excinfo:
        tpm_bypass.patch_boot_wim_on_usb("E")

    message = str(excinfo.value)
    assert "labconfig denied" in message
    assert "could not unmount" in message
    assert excinfo.value.cleanup_ok is False
    assert "cleanup-wim" in message


@patch("core.tpm_bypass.os.path.isfile", return_value=True)
@patch("core.tpm_bypass._unmount")
@patch("core.tpm_bypass._inject_registry")
@patch("core.tpm_bypass._mount")
def test_patch_boot_wim_flags_leftover_hive_for_retry(
    mock_mount: MagicMock,
    mock_inject: MagicMock,
    mock_unmount: MagicMock,
    mock_isfile: MagicMock,
) -> None:
    mock_inject.side_effect = tpm_bypass.TpmBypassError(
        "HKLM\\OFFLINE could not be unloaded", cleanup_ok=False
    )

    with pytest.raises(tpm_bypass.TpmBypassError) as excinfo:
        tpm_bypass.patch_boot_wim_on_usb("E")

    assert excinfo.value.cleanup_ok is False
    assert "reg unload HKLM\\OFFLINE" in str(excinfo.value)


@patch("core.tpm_bypass.subprocess.run")
def test_unload_hive_retries_then_gives_up(
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tpm_bypass, "_UNLOAD_RETRY_DELAY", 0)
    mock_run.side_effect = tpm_bypass.subprocess.CalledProcessError(
        1, ["reg.exe"], stderr="in use"
    )

    error = tpm_bypass._unload_hive("HKLM\\OFFLINE")

    assert isinstance(error, tpm_bypass.subprocess.CalledProcessError)
    assert mock_run.call_count == tpm_bypass._UNLOAD_RETRIES

