"""Tests for core.paths — file lock degradation (D11)."""

import logging
import sys

from core.paths import file_lock


def test_file_lock_acquires_and_releases_cleanly(tmp_path, caplog):
    """Happy path: the lock is taken and released, no degradation warning."""
    caplog.set_level(logging.WARNING, logger="flint")

    with file_lock(tmp_path / "a.lock"):
        pass
    # A second acquisition proves the first was actually released.
    with file_lock(tmp_path / "a.lock", retries=0, delay=0):
        pass

    assert "proceeding without a lock" not in caplog.text


def test_file_lock_proceeds_without_lock_when_msvcrt_missing(
    tmp_path, monkeypatch, caplog
):
    """D11: locking unavailable (non-Windows / no msvcrt) → warn and run
    the body without a lock instead of raising ImportError."""
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    caplog.set_level(logging.WARNING, logger="flint")

    ran = False
    with file_lock(tmp_path / "b.lock", retries=0, delay=0):
        ran = True

    assert ran is True
    assert "proceeding without a lock" in caplog.text


def test_file_lock_proceeds_without_lock_when_retries_exhausted(
    tmp_path, monkeypatch, caplog
):
    """D11: another process holding the lock → warn and run without it
    instead of re-raising the final OSError."""
    import msvcrt

    def always_denied(fd, mode, nbytes):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(msvcrt, "locking", always_denied)
    caplog.set_level(logging.WARNING, logger="flint")

    ran = False
    with file_lock(tmp_path / "c.lock", retries=2, delay=0):
        ran = True

    assert ran is True
    assert "proceeding without a lock" in caplog.text


def test_file_lock_proceeds_when_lock_file_cannot_be_opened(tmp_path, caplog):
    """D11: even the sentinel file itself failing to open degrades to a
    warning instead of blowing up the caller."""
    caplog.set_level(logging.WARNING, logger="flint")

    ran = False
    with file_lock(tmp_path, retries=0, delay=0):  # tmp_path is a directory
        ran = True

    assert ran is True
    assert "proceeding without a lock" in caplog.text
