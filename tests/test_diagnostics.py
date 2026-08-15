"""Diagnostics bundle tests: the pure composer in core.diagnostics."""

from core.diagnostics import build_diagnostics


def _drives() -> list[dict]:
    return [
        {
            "model": "USB DISK",
            "size_gb": 32,
            "letters": ["E:"],
            "bus_type": "USB",
            "serial": "SERIAL1",
            "physical_path": r"\\.\PHYSICALDRIVE1",
        },
        {
            "model": "SD Card",
            "size_gb": 64,
            "letter": "F:",
            "letters": ["F:", "G:"],
            "bus_type": "USB",
            "serial": "",
            "physical_path": r"\\.\PHYSICALDRIVE2",
        },
    ]


def test_diagnostics_includes_version_and_platform():
    text = build_diagnostics(_drives(), elevated=True, entries=[])

    assert text.startswith("Flint diagnostics")
    assert "Version:" in text
    assert "Python:" in text
    assert "Platform:" in text
    assert "Elevated: yes" in text


def test_diagnostics_lists_drives_with_only_safe_fields():
    text = build_diagnostics(_drives(), elevated=False, entries=[])

    assert "USB DISK" in text
    assert "32 GB" in text
    assert r"\\.\PHYSICALDRIVE1" in text
    assert "SERIAL1" in text
    assert "E:" in text
    assert "F:, G:" in text
    assert "Drives (2):" in text


def test_diagnostics_includes_recent_history():
    entries = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "iso": "test.iso",
            "drive": "USB DISK",
            "success": True,
            "verified": True,
            "wipe_verified": "not run",
        }
    ]
    text = build_diagnostics(_drives(), entries=entries)

    assert "History (last 1 of 1):" in text
    assert "test.iso -> USB DISK" in text
    assert "success=True" in text
    assert "wipe_verified=not run" in text


def test_diagnostics_handles_empty_state():
    text = build_diagnostics([], elevated=None, entries=[])

    assert "Drives (0):" in text
    assert "History (last 0 of 0):" in text
    assert "Elevated: no" in text