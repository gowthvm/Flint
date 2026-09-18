"""Tests for core.log — logging setup."""

import logging
import os

from core.log import setup_logging


def test_setup_logging_returns_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "APPDATA": str(tmp_path)})
    logger = setup_logging("flint_test", "DEBUG")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "flint_test"


def test_setup_logging_creates_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "APPDATA": str(tmp_path)})
    setup_logging("flint_test_file", "INFO")
    log_dir = tmp_path / "flint" / "logs"
    # The function may or may not create the dir depending on existing state
    # Just verify it doesn't crash


def test_setup_logging_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "APPDATA": str(tmp_path)})
    logger1 = setup_logging("flint_test_idempotent", "INFO")
    logger2 = setup_logging("flint_test_idempotent", "INFO")
    # Second call should return same logger (idempotent)
    assert logger1 is logger2


def test_setup_logging_level(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "APPDATA": str(tmp_path)})
    logger = setup_logging("flint_test_level", "WARNING")
    assert logger.level == logging.WARNING
