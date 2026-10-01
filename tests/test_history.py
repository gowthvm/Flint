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


# ---------------------------------------------------------------------------
# U13: load_history caches the parsed file, keyed by (path, mtime, size)
# ---------------------------------------------------------------------------


def test_repeated_load_history_parses_the_file_once(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "HISTORY_PATH", tmp_path / "h.json")
    h.save_history([{"iso": "a.iso"}])

    real_load = json.load
    calls: list[str] = []

    def counting_load(fp, *args, **kwargs):
        calls.append(getattr(fp, "name", "?"))
        return real_load(fp, *args, **kwargs)

    monkeypatch.setattr(h.json, "load", counting_load)

    first = h.load_history()
    second = h.load_history()

    assert first == second == [{"iso": "a.iso"}]
    assert len(calls) == 1


def test_cache_sees_a_change_written_behind_its_back(
    tmp_path, monkeypatch
):
    """A writer that bypasses this module (another process, an import)
    must still be picked up on the next read."""
    path = tmp_path / "h.json"
    monkeypatch.setattr(h, "HISTORY_PATH", path)
    h.save_history([{"iso": "one.iso"}])
    assert [e["iso"] for e in h.load_history()] == ["one.iso"]

    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "entries": [{"iso": "one.iso"}, {"iso": "two.iso"}],
            }
        ),
        encoding="utf-8",
    )

    assert [e["iso"] for e in h.load_history()] == ["one.iso", "two.iso"]


def test_mutating_the_returned_list_does_not_leak_into_the_cache(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(h, "HISTORY_PATH", tmp_path / "h.json")
    h.save_history([{"iso": "a.iso"}])

    entries = h.load_history()
    entries.append({"iso": "b.iso"})
    entries.clear()

    assert [e["iso"] for e in h.load_history()] == ["a.iso"]


def test_cache_is_scoped_to_the_history_path(tmp_path, monkeypatch):
    other = tmp_path / "other.json"
    monkeypatch.setattr(h, "HISTORY_PATH", tmp_path / "h.json")
    h.save_history([{"iso": "first.iso"}])
    assert [e["iso"] for e in h.load_history()] == ["first.iso"]

    monkeypatch.setattr(h, "HISTORY_PATH", other)
    h.save_history([{"iso": "second.iso"}, {"iso": "third.iso"}])
    assert [e["iso"] for e in h.load_history()] == [
        "second.iso",
        "third.iso",
    ]

    monkeypatch.setattr(h, "HISTORY_PATH", tmp_path / "h.json")
    assert [e["iso"] for e in h.load_history()] == ["first.iso"]


def test_saving_refreshes_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "HISTORY_PATH", tmp_path / "h.json")
    h.save_history([{"iso": "a.iso"}])
    assert len(h.load_history()) == 1

    h.save_history([{"iso": "b.iso"}, {"iso": "c.iso"}])

    assert [e["iso"] for e in h.load_history()] == ["b.iso", "c.iso"]
    h.append_history({"iso": "d.iso"})
    assert [e["iso"] for e in h.load_history()] == [
        "b.iso",
        "c.iso",
        "d.iso",
    ]


def test_missing_file_is_cached_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "HISTORY_PATH", tmp_path / "absent.json")
    assert h.load_history() == []
    assert h.load_history() == []


# ---------------------------------------------------------------------------
# L09: flash_report labels the operation instead of hard-coding "flash"
# ---------------------------------------------------------------------------


def test_flash_report_defaults_to_flash_operation():
    report = h.flash_report("img.iso", "Kingston", 12.0, True, True)
    assert report["operation"] == "flash"


def test_flash_report_accepts_a_custom_operation():
    report = h.flash_report("img.iso", "Kingston", 1.0, True, True,
                            operation="wipe")
    assert report["operation"] == "wipe"
    assert report["iso"] == "img.iso"


def test_flash_report_records_the_physical_path():
    """Lets a serial-less stick be recognised later (core.fleet)."""
    report = h.flash_report(
        "img.iso", "Generic", 1.0, True, True,
        drive_path=r"\\.\PHYSICALDRIVE3",
    )
    assert report["physical_path"] == r"\\.\PHYSICALDRIVE3"


def test_flash_report_path_defaults_to_none():
    report = h.flash_report("img.iso", "Generic", 1.0, True, True)
    assert report["physical_path"] is None



def test_async_append_visible_before_disk_lands(tmp_path, monkeypatch):
    """U15: pending entries are served to readers immediately; the queued
    job lands them on disk and clears the overlay."""
    monkeypatch.setattr(h, "HISTORY_PATH", Path(tmp_path) / "h.json")
    monkeypatch.setattr(h, "_pending_entries", None)
    jobs = []
    monkeypatch.setattr(h.writeback, "enabled", lambda: True)
    monkeypatch.setattr(h.writeback, "submit", jobs.append)

    h.append_history({"iso": "x.iso"})

    entries = h.load_history()
    assert [e["iso"] for e in entries] == ["x.iso"]  # overlay, not disk
    assert not h.HISTORY_PATH.exists()

    jobs[0]()  # the worker job: merge on disk + clear pending
    assert h.HISTORY_PATH.exists()
    assert [e["iso"] for e in h.load_history()] == ["x.iso"]


def test_async_append_audited_returns_chained_record(tmp_path, monkeypatch):
    """U15: the audited record is chained and returned synchronously while
    the write itself is deferred."""
    monkeypatch.setattr(h, "HISTORY_PATH", Path(tmp_path) / "h.json")
    monkeypatch.setattr(h, "_pending_entries", None)
    jobs = []
    monkeypatch.setattr(h.writeback, "enabled", lambda: True)
    monkeypatch.setattr(h.writeback, "submit", jobs.append)

    rec = h.append_audited_history({"iso": "y.iso", "success": True})
    assert rec.get("integrity_sha256")
    assert h.load_history()[0]["integrity_sha256"] == rec["integrity_sha256"]

    jobs[0]()
    on_disk = json.loads(h.HISTORY_PATH.read_text(encoding="utf-8"))
    assert on_disk["entries"][0]["integrity_sha256"] == rec["integrity_sha256"]
    assert h.verify_history_integrity() == (True, None)


def test_async_save_older_job_cannot_clear_newer_pending(
    tmp_path, monkeypatch
):
    """U15: the sequence guard keeps an older job from dropping a newer
    pending list when saves queue up faster than they land."""
    monkeypatch.setattr(h, "HISTORY_PATH", Path(tmp_path) / "h.json")
    monkeypatch.setattr(h, "_pending_entries", None)
    jobs = []
    monkeypatch.setattr(h.writeback, "enabled", lambda: True)
    monkeypatch.setattr(h.writeback, "submit", jobs.append)

    h.save_history([{"iso": "first.iso"}])
    h.save_history([{"iso": "second.iso"}])
    assert [e["iso"] for e in h.load_history()] == ["second.iso"]

    jobs[0]()  # older job lands first; must not clear the newer pending
    assert [e["iso"] for e in h.load_history()] == ["second.iso"]
    jobs[1]()
    assert [e["iso"] for e in h.load_history()] == ["second.iso"]
    on_disk = json.loads(h.HISTORY_PATH.read_text(encoding="utf-8"))
    assert [e["iso"] for e in on_disk["entries"]] == ["second.iso"]


# ---------------------------------------------------------------------------
# H1: a store that cannot be decoded is quarantined, never silently replaced
# ---------------------------------------------------------------------------


def test_corrupt_history_is_quarantined_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "h.json"
    monkeypatch.setattr(h, "HISTORY_PATH", path)
    path.write_text("{ truncated", encoding="utf-8")

    assert h.load_history() == []

    leftovers = list(tmp_path.glob("h.json.corrupt-*"))
    assert len(leftovers) == 1
    assert leftovers[0].read_text(encoding="utf-8") == "{ truncated"
    assert not path.exists()

    h.append_history({"iso": "a.iso"})
    assert [e["iso"] for e in h.load_history()] == ["a.iso"]


def test_corrupt_history_that_cannot_be_quarantined_is_never_cached(
    tmp_path, monkeypatch
):
    path = tmp_path / "h.json"
    monkeypatch.setattr(h, "HISTORY_PATH", path)
    path.write_text("{ truncated", encoding="utf-8")

    def denied(self, target):
        raise OSError("access denied")

    monkeypatch.setattr(Path, "replace", denied)
    try:
        assert h.load_history() == []
        # A failed read must never be cached: the file key still matches,
        # so caching "empty" here would hide the damage from every later
        # call and let verify_history_integrity() pass vacuously.
        assert h._history_cache is None

        h.append_history({"iso": "a.iso"})

        # Refused: the only copy of the damaged store is still on disk.
        assert path.read_text(encoding="utf-8") == "{ truncated"
    finally:
        h._history_cache = None


# ---------------------------------------------------------------------------
# C2: CSV export must accept records with different keys
# ---------------------------------------------------------------------------


def test_export_history_csv_unions_mixed_entry_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "HISTORY_PATH", tmp_path / "h.json")
    h.save_history(
        [
            {"timestamp": "t1", "iso": "a.iso"},
            {"timestamp": "t2", "iso": "b.iso", "integrity_sha256": "ff"},
            {"timestamp": "t3", "iso": None, "operation": "wipe"},
        ]
    )

    out = tmp_path / "h.csv"
    assert h.export_history_csv(out) is True

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # header + 3 rows
    assert "integrity_sha256" in lines[0]
