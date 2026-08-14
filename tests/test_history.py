import json
from pathlib import Path

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


def test_history_import_filters_non_dict_entries(tmp_path):
    """L7 regression: non-dict entries in an imported history file must be
    dropped, not crash rendering."""
    h.HISTORY_PATH = Path(tmp_path) / "h.json"
    h.clear_history()
    src = tmp_path / "import.json"
    src.write_text(
        json.dumps(
            [
                {"success": True, "schema_version": 2, "iso": "a.iso"},
                "garbage",
                42,
                None,
                {"success": True, "schema_version": 2, "iso": "b.iso"},
            ]
        ),
        encoding="utf-8",
    )
    ok, count = h.import_history(src)
    assert ok and count == 2
    entries = h.load_history()
    assert [e["iso"] for e in entries] == ["a.iso", "b.iso"]

