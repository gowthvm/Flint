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
