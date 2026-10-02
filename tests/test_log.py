"""Tests for core.log — logging setup."""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import pytest

from core.log import apply_log_level, setup_cli_logging, setup_logging


def test_setup_logging_returns_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "APPDATA": str(tmp_path)})
    logger = setup_logging("flint_test", "DEBUG")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "flint_test"


def test_setup_logging_creates_log_file(tmp_path, monkeypatch):
    """setup_logging must actually attach a rotating file handler that
    receives records — the old version of this test asserted nothing and
    passed no matter what."""
    monkeypatch.setattr(os, "environ", {**os.environ, "APPDATA": str(tmp_path)})
    name = "flint_test_file"
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    try:
        setup_logging(name, "INFO")

        files = [
            h
            for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert files, "setup_logging must attach a rotating file handler"
        assert files[0].baseFilename.endswith(f"{name}-startup.log")

        logger.info("file handler marker")
        for handler in logger.handlers:
            handler.flush()
        logged = Path(files[0].baseFilename).read_text(encoding="utf-8")
        assert "file handler marker" in logged
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


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


def test_setup_logging_writes_into_app_dir(tmp_path, monkeypatch):
    """C02: the startup log belongs in APP_DIR, not in %TEMP%."""
    from core import paths

    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setenv("TEMP", str(temp_dir))
    name = "flint_test_appdir"
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    try:
        setup_logging(name, "INFO")
        logger.info("app-dir marker")
        for handler in logger.handlers:
            handler.flush()
        target = paths.APP_DIR / f"{name}-startup.log"
        assert target.exists(), "C02: the startup log must go to APP_DIR"
        assert "app-dir marker" in target.read_text(encoding="utf-8")
        assert not list(temp_dir.iterdir()), "nothing may land in %TEMP%"
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def _fresh_cli_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    return logger


def test_setup_cli_logging_idempotent_and_suppresses_last_resort(monkeypatch):
    """L21: one stderr handler at WARNING, lastResort disabled, safe to
    call twice."""
    monkeypatch.setattr(logging, "lastResort", logging.lastResort)
    name = "flint_cli_test_idempotent"
    logger = _fresh_cli_logger(name)

    first = setup_cli_logging(name)
    second = setup_cli_logging(name)

    assert first is second
    cli_handlers = [
        h
        for h in logger.handlers
        if getattr(h, "_flint_cli_handler", False)
    ]
    assert len(cli_handlers) == 1, "handler must not be duplicated"
    handler = cli_handlers[0]
    assert handler.level == logging.WARNING
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr
    assert logging.lastResort is None


def test_setup_cli_logging_only_passes_warnings_and_above(
    monkeypatch, capsys
):
    """The stderr handler sits at WARNING so INFO noise never reaches the
    console next to stdout data."""
    name = "flint_cli_test_levels"
    logger = _fresh_cli_logger(name)
    setup_cli_logging(name)

    logger.info("quiet info")
    logger.warning("loud warning")

    captured = capsys.readouterr()
    assert "quiet info" not in captured.err
    assert "loud warning" in captured.err


def test_apply_log_level_sets_logger_level():
    name = "flint_level_test"
    try:
        assert apply_log_level("debug", name=name) == "DEBUG"
        assert logging.getLogger(name).level == logging.DEBUG
        assert apply_log_level("  warning ", name=name) == "WARNING"
        assert logging.getLogger(name).level == logging.WARNING
    finally:
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_apply_log_level_default_target_is_flint_logger():
    previous = logging.getLogger("flint").level
    try:
        assert apply_log_level("info") == "INFO"
        assert logging.getLogger("flint").level == logging.INFO
    finally:
        logging.getLogger("flint").setLevel(previous)


def test_apply_log_level_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown log level"):
        apply_log_level("LOUDER", name="flint_level_test")
    with pytest.raises(ValueError, match="unknown log level"):
        apply_log_level("", name="flint_level_test")
