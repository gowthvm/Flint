import os
from pathlib import Path
import tempfile
import core.history as h


def test_history_export_import(tmp_path):
    d = tmp_path
    h.HISTORY_PATH = Path(d) / "h.json"
    h.clear_history()
    h.append_history({"success": True, "schema_version": 2, "iso": "x.iso", "bootable": True, "avg_mbps": 12.5})
    entries = h.load_history()
    assert len(entries) == 1
    assert entries[0]["schema_version"] == 2
    exp = d / "exp.json"
    assert h.export_history(exp)
    h.clear_history()
    assert h.load_history() == []
    ok, count = h.import_history(exp)
    assert ok and count == 1

