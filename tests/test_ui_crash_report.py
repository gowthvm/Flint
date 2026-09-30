"""U16: the crash-report offer reads only the tail of an unbounded log
and remembers how far the user has already seen."""

from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _FakeSettings:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set_many(self, **values):
        self.data.update(values)


def _harness(monkeypatch, tmp_path):
    import main

    crash = tmp_path / "crash.log"
    monkeypatch.setattr(main, "_CRASH_PATH", str(crash))
    settings = _FakeSettings()
    dialogs_shown: list[dict] = []

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            dialogs_shown.append(kwargs)
            self._kwargs = kwargs

        def run(self):
            return "copy"

    dialogs = SimpleNamespace(FlintDialog=_FakeDialog)
    copied: list[str] = []
    clipboard = SimpleNamespace(setText=copied.append)
    application = SimpleNamespace(clipboard=lambda: clipboard)
    return main, crash, settings, dialogs, dialogs_shown, copied, application


def test_missing_crash_log_is_silent(qapp, tmp_path, monkeypatch):
    main, _crash, settings, dialogs, shown, _copied, application = (
        _harness(monkeypatch, tmp_path)
    )
    main._maybe_show_crash_report(None, settings, dialogs, application)
    assert shown == []
    assert settings.data == {}


def test_crash_report_reads_only_the_tail(qapp, tmp_path, monkeypatch):
    main, crash, settings, dialogs, shown, copied, application = (
        _harness(monkeypatch, tmp_path)
    )
    crash.write_bytes(b"A" * 200_000 + b"Last traceback line\n")
    size = crash.stat().st_size
    main._maybe_show_crash_report(None, settings, dialogs, application)
    assert len(shown) == 1
    # The whole offer persists what was on disk, not what was read.
    assert settings.data["crash_report_seen_bytes"] == size
    assert len(copied) == 1
    # Only the tail is ever decoded - at most _CRASH_TAIL_BYTES.
    assert len(copied[0].encode("utf-8")) <= main._CRASH_TAIL_BYTES
    assert copied[0].endswith("Last traceback line")
    # Nothing new -> no second dialog on the next startup.
    main._maybe_show_crash_report(None, settings, dialogs, application)
    assert len(shown) == 1


def test_growth_below_the_first_read_is_not_reprompted(
    qapp, tmp_path, monkeypatch
):
    main, crash, settings, dialogs, shown, _copied, application = (
        _harness(monkeypatch, tmp_path)
    )
    crash.write_bytes(b"first crash\n")
    main._maybe_show_crash_report(None, settings, dialogs, application)
    assert len(shown) == 1
    # Rewrite identical content: size unchanged -> no dialog.
    crash.write_bytes(b"first crash\n")
    main._maybe_show_crash_report(None, settings, dialogs, application)
    assert len(shown) == 1
    # Genuine growth -> offered again, and the offset advances.
    with crash.open("ab") as handle:
        handle.write(b"second crash\n")
    main._maybe_show_crash_report(None, settings, dialogs, application)
    assert len(shown) == 2
    assert settings.data["crash_report_seen_bytes"] == crash.stat().st_size
