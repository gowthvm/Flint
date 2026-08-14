import json
from pathlib import Path

import core.settings as s


def test_settings_defaults(tmp_path):
    s.SETTINGS_PATH = Path(tmp_path) / "s.json"
    # reload module cache
    # Ensure get returns default values
    assert s.get("theme") == "dark"
    assert s.get("verify_after_write") is True
    s.set_many(theme="light", window_geometry="W1000H700")
    assert s.get("theme") == "light"
    assert s.get("window_geometry") == "W1000H700"


def test_settings_drops_wrong_typed_values_on_load(tmp_path):
    """L6 regression: corrupted/hand-edited settings of the wrong type
    must fall back to defaults instead of crashing startup."""
    path = Path(tmp_path) / "s.json"
    path.write_text(
        json.dumps(
            {
                "theme": "light",
                "verify_after_write": "yes",
                "window_geometry": 42,
                "chunk_size_mb": "8",
                "expert_mode": 1,
                "onboarding_seen": "true",
            }
        ),
        encoding="utf-8",
    )
    s.SETTINGS_PATH = path
    s._CACHE = None
    try:
        assert s.get("theme") == "light"
        assert s.get("verify_after_write") is True
        assert s.get("window_geometry") is None
        assert s.get("chunk_size_mb") == 8
        assert s.get("expert_mode") is True
        assert s.get("onboarding_seen") is False
    finally:
        s._CACHE = None
