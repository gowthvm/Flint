"""Shared test fixtures for the Flint test suite."""

import sys

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Redirect APP_DIR to a temp directory so tests never touch real settings."""
    from core import paths, settings

    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "APP_DIR", app_dir)
    monkeypatch.setattr(settings, "SETTINGS_PATH", app_dir / "settings.json")
    monkeypatch.setattr(settings, "_LOCK_PATH", app_dir / "settings.lock")
    settings._CACHE = None
    return app_dir


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    """Redirect core.paths.APP_DIR so queue persistence never touches the
    real %APPDATA%\\Flint. Separate fixture name so test modules that
    shadow ``_isolated_settings`` cannot accidentally undo this."""
    from core import paths

    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "APP_DIR", app_dir)
    return app_dir


@pytest.fixture(autouse=True)
def _isolated_system_disk_cache(monkeypatch):
    """Reset drives._OTHER_SYSTEM_CACHE before every test.

    It is a process-lifetime cache in production; without this, one test
    that resolves real registry/WMI data (e.g. cli flash -> _is_system_disk)
    leaks the host's physical paths into later tests' exclusion sets."""
    from core import drives

    monkeypatch.setattr(drives, "_OTHER_SYSTEM_CACHE", None)


@pytest.fixture()
def _make_window(monkeypatch):
    """Create a MainWindow without showing it (skips elevation checks)."""

    def _make():
        from unittest.mock import patch

        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)

        with patch("core.drives.DrivePoller"), patch(
            "core.drives.DriveDetector"
        ):
            from ui.window import MainWindow

            w = MainWindow()
            w.hide()
            return w

    return _make
