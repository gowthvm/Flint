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


def test_auto_eject_defaults_false_and_typechecked(tmp_path):
    """v1.9.0: auto_eject is off by default and rejects bad types."""
    assert s.get("auto_eject") is False
    path = Path(tmp_path) / "s.json"
    path.write_text(json.dumps({"auto_eject": "yes"}), encoding="utf-8")
    s.SETTINGS_PATH = path
    s._CACHE = None
    try:
        assert s.get("auto_eject") is False
        s.set_many(auto_eject=True)
        assert s.get("auto_eject") is True
    finally:
        s._CACHE = None


def test_dead_export_import_api_removed():
    """C03: export_settings/import_settings were never called anywhere."""
    assert not hasattr(s, "export_settings")
    assert not hasattr(s, "import_settings")


def test_legacy_settings_file_loads_and_gets_stamped_on_save(_isolated_settings):
    """C10: a pre-versioned settings.json still loads (defaults merge) and
    the next save stamps the current schema_version."""
    path = _isolated_settings / "settings.json"
    path.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    s._CACHE = None
    try:
        assert s.get("theme") == "light"
        assert s.get("schema_version") == s.SETTINGS_SCHEMA_VERSION == 1
        assert s.get("expert_mode") is True  # defaults still merged in

        s.set_many(theme="dark")

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["schema_version"] == 1
        assert on_disk["theme"] == "dark"
    finally:
        s._CACHE = None


def test_older_schema_version_is_migrated_on_load(_isolated_settings):
    """C10: a version-0 (or corrupted-version) file is run through the
    migration dispatch and stamped with the current version in memory."""
    path = _isolated_settings / "settings.json"
    path.write_text(
        json.dumps({"schema_version": 0, "onboarding_seen": True}),
        encoding="utf-8",
    )
    s._CACHE = None
    try:
        assert s.get("onboarding_seen") is True
        assert s.get("schema_version") == 1
    finally:
        s._CACHE = None


def test_settings_save_always_stamps_current_schema_version(_isolated_settings):
    """C10: callers cannot talk the stored version down."""
    s._CACHE = None
    try:
        s.set_many(schema_version=0, theme="solarized")
        assert s.get("schema_version") == 1
        on_disk = json.loads(
            (_isolated_settings / "settings.json").read_text(encoding="utf-8")
        )
        assert on_disk["schema_version"] == 1
    finally:
        s._CACHE = None


def test_settings_from_a_newer_flint_keep_their_version(_isolated_settings):
    """Downgrading Flint must not fake the schema version.

    A v2 file read by a v1 build used to be stamped 1 on load *and* on
    every save, so upgrading again would run the 1->2 migration over data
    that had already been migrated.
    """
    path = _isolated_settings / "settings.json"
    path.write_text(
        json.dumps(
            {"schema_version": 99, "theme": "light", "future_key": "kept"}
        ),
        encoding="utf-8",
    )
    s._CACHE = None
    try:
        assert s.get("schema_version") == 99
        assert s.get("future_key") == "kept"

        s.set_many(theme="dark")

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["schema_version"] == 99
        assert on_disk["future_key"] == "kept"
        assert on_disk["theme"] == "dark"
    finally:
        s._CACHE = None


def test_fleet_skip_flashed_defaults_false(_isolated_settings):
    """U21: the fleet skip toggle persists (support-only: the UI wires the
    read/write in a later phase)."""
    assert "fleet_skip_flashed" in s._DEFAULTS
    assert s.get("fleet_skip_flashed") is False

    s.set_many(fleet_skip_flashed=True)
    assert s.get("fleet_skip_flashed") is True
    on_disk = json.loads(
        (_isolated_settings / "settings.json").read_text(encoding="utf-8")
    )
    assert on_disk["fleet_skip_flashed"] is True

    s.set_many(fleet_skip_flashed="yes")  # wrong type -> rejected
    assert s.get("fleet_skip_flashed") is True


def test_set_many_async_visible_immediately_and_job_lands_it(
    _isolated_settings, monkeypatch
):
    """U15: with write-back enabled the cache updates synchronously while
    the durable write is deferred; running the queued job lands it."""
    jobs = []
    monkeypatch.setattr(s.writeback, "enabled", lambda: True)
    monkeypatch.setattr(s.writeback, "submit", jobs.append)

    s.set_many(theme="light")

    assert s.get("theme") == "light"  # visible immediately
    path = Path(s.SETTINGS_PATH)
    # The fsync has not run: either the file does not exist yet or it
    # still holds the pre-save content.
    assert not path.exists() or (
        json.loads(path.read_text(encoding="utf-8")).get("theme") != "light"
    )
    assert len(jobs) == 1

    jobs[0]()  # the deferred fsync + replace
    on_disk = json.loads(Path(s.SETTINGS_PATH).read_text(encoding="utf-8"))
    assert on_disk.get("theme") == "light"
    assert s.get("theme") == "light"


def test_set_many_lands_after_flush_with_real_writeback(_isolated_settings):
    """U15 end-to-end: enable the real worker, flush, file is written."""
    from core import writeback

    try:
        writeback.enable()
        s.set_many(theme="light")
        assert s.get("theme") == "light"
        assert writeback.flush(5.0) is True
        on_disk = json.loads(Path(s.SETTINGS_PATH).read_text(encoding="utf-8"))
        assert on_disk.get("theme") == "light"
    finally:
        writeback.disable()


# ---------------------------------------------------------------------------
# S1: a settings file we cannot decode is quarantined, not replaced
# ---------------------------------------------------------------------------


def test_corrupt_settings_are_quarantined_not_overwritten(_isolated_settings):
    path = Path(s.SETTINGS_PATH)
    path.write_text("{ this is not json", encoding="utf-8")
    s._CACHE = None
    try:
        # We run on defaults for this session...
        assert s.get("theme") == "dark"
        # ...but the damaged bytes were moved aside, not overwritten.
        assert not path.exists()
        leftovers = list(_isolated_settings.glob("settings.json.corrupt-*"))
        assert len(leftovers) == 1
        assert (
            leftovers[0].read_text(encoding="utf-8")
            == "{ this is not json"
        )

        # Once it is quarantined the store is genuinely empty again, so a
        # later save may publish a fresh one.
        s.set_many(theme="light")
        assert json.loads(path.read_text(encoding="utf-8"))["theme"] == "light"
    finally:
        s._CACHE = None


def test_unreadable_settings_refuse_the_save_that_would_clobber_them(
    _isolated_settings, monkeypatch
):
    path = Path(s.SETTINGS_PATH)
    path.write_text("{ this is not json", encoding="utf-8")
    s._CACHE = None

    def denied(self, target):
        raise OSError("access denied")

    monkeypatch.setattr(Path, "replace", denied)
    try:
        assert s.get("theme") == "dark"
        assert path.exists()  # quarantine failed, bytes are still here

        s.set_many(theme="light")

        assert path.read_text(encoding="utf-8") == "{ this is not json"
    finally:
        s._CACHE = None


# ---------------------------------------------------------------------------
# S2: only the keys this call changed are written back
# ---------------------------------------------------------------------------


def test_set_many_does_not_clobber_untouched_keys(_isolated_settings):
    """A full snapshot made the read-merge-write in _persist_snapshot a
    no-op: every key we held was written back, so a change another Flint
    instance made to a key we never touched was silently reverted."""
    path = Path(s.SETTINGS_PATH)
    s._CACHE = None
    try:
        assert s.get("theme") == "dark"  # our process loaded defaults

        # A second instance writes its own choice for a key we never set.
        path.write_text(
            json.dumps({"schema_version": 1, "theme": "light"}),
            encoding="utf-8",
        )

        s.set_many(auto_eject=True)

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["theme"] == "light"
        assert on_disk["auto_eject"] is True
    finally:
        s._CACHE = None
