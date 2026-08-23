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
