"""Tests for main.py — pure helper functions."""

import os
import sys
from unittest.mock import patch

from main import _is_launcher_exe, _windowless_python


def test_windowless_python_returns_string():
    result = _windowless_python()
    assert isinstance(result, str)
    assert "python" in result.lower() or "pythonw" in result.lower()


def test_windowless_python_prefers_pythonw(monkeypatch):
    fake_pythonw = "C:\\Python312\\pythonw.exe"
    fake_python = "C:\\Python312\\python.exe"
    monkeypatch.setattr(sys, "executable", fake_python)

    with patch("os.path.isfile", side_effect=lambda p: "pythonw" in p):
        result = _windowless_python()
        assert "pythonw" in result


def test_is_launcher_exe_false_for_nonexistent(tmp_path):
    fake = str(tmp_path / "nonexistent.exe")
    assert _is_launcher_exe(fake) is False


def test_is_launcher_exe_true_when_exe_exists(tmp_path):
    exe = tmp_path / "flint.exe"
    exe.write_bytes(b"fake")
    assert _is_launcher_exe(str(exe)) is True


def test_is_launcher_exe_false_for_non_exe(tmp_path):
    txt = tmp_path / "flint.txt"
    txt.write_bytes(b"fake")
    assert _is_launcher_exe(str(txt)) is False
