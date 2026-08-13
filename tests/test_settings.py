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
