"""U09: the drop zone remembers recent images and unfinished writes
from a previous session come back through a resume banner."""

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    import core.settings as s

    original = s.SETTINGS_PATH
    s.SETTINGS_PATH = tmp_path / "s.json"
    s._CACHE = None
    yield
    s._CACHE = None
    s.SETTINGS_PATH = original


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    from ui import dialogs

    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(dialogs, "completion", lambda *a, **k: None)
    monkeypatch.setattr(dialogs, "inform", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _open_system_disk_guard(monkeypatch):
    from core.drives import DriveDetector

    monkeypatch.setattr(
        DriveDetector, "_system_disk_paths", lambda self: set()
    )


def _make_window(qapp, tmp_path):
    import core.settings as s

    s.set_many(onboarding_seen=True, theme="dark")
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def _write(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"x" * 32)
    return str(path)


def test_remember_recent_persists_and_dedupes(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    first = _write(tmp_path, "one.iso")
    second = _write(tmp_path, "two.iso")
    try:
        w._remember_recent(first)
        assert s.get("recent_images") == [first]
        w._remember_recent(second)
        assert s.get("recent_images") == [second, first]
        w._remember_recent(first)
        assert s.get("recent_images") == [first, second]
        zone = w._iso_zone
        assert [b.text() for b in zone._recent_buttons] == [
            "one.iso",
            "two.iso",
        ]
        assert zone._recent_buttons[0].toolTip() == first
        # Paths that are not files never enter the list.
        w._remember_recent(str(tmp_path / "missing.iso"))
        assert s.get("recent_images") == [first, second]
    finally:
        w._shutdown()


def test_remember_recent_caps_at_five(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        paths = [_write(tmp_path, f"img{i}.iso") for i in range(6)]
        for path in paths:
            w._remember_recent(path)
        recent = s.get("recent_images")
        assert recent == list(reversed(paths))[:5]
        assert len(w._iso_zone._recent_buttons) <= 3
    finally:
        w._shutdown()


def test_recents_restore_on_startup(qapp, tmp_path):
    import core.settings as s

    saved = _write(tmp_path, "saved.iso")
    s.set_many(recent_images=[saved])
    w = _make_window(qapp, tmp_path)
    try:
        zone = w._iso_zone
        assert [b.text() for b in zone._recent_buttons] == ["saved.iso"]
        assert zone._recent_buttons[0].toolTip() == saved
    finally:
        w._shutdown()


def _save_manifest(app_dir, source, source_size=None, state="resumable"):
    from pathlib import Path

    from core import jobs

    src = Path(str(source))
    manifest = jobs.JobManifest(
        source_path=str(src),
        source_size=(
            source_size if source_size is not None else src.stat().st_size
        ),
        source_sha256="a" * 64,
        target_fingerprint="SER123",
        target_size=1_000_000,
        options={},
        state=state,
    )
    path = app_dir / "jobs" / f"{manifest.job_id}.json"
    jobs.save_manifest(path, manifest)
    return manifest


def test_unfinished_write_banner_feeds_the_queue(
    qapp, tmp_path, monkeypatch
):
    from ui import window as window_mod

    app_dir = tmp_path / "app"
    (app_dir / "jobs").mkdir(parents=True)
    image = _write(tmp_path, "os.img")
    _save_manifest(app_dir, image)
    monkeypatch.setattr(window_mod, "APP_DIR", app_dir)
    w = _make_window(qapp, tmp_path)
    try:
        # The scan runs during startup wiring.
        assert not w._resume_banner.isHidden()
        assert "1 unfinished write" in w._resume_label.text()
        w._on_resume_add_clicked()
        assert w._queue_images() == [image]
        assert w._resume_banner.isHidden()
        # Adding again is a no-op - the image is already queued.
        w._scan_unfinished_jobs()
        assert not w._resume_banner.isHidden()
        w._on_resume_add_clicked()
        assert w._queue_images() == [image]
        assert w._resume_banner.isHidden()
    finally:
        w._shutdown()


def test_banner_ignores_manifests_whose_image_changed(
    qapp, tmp_path, monkeypatch
):
    from ui import window as window_mod

    app_dir = tmp_path / "app"
    (app_dir / "jobs").mkdir(parents=True)
    image = _write(tmp_path, "os.img")
    _save_manifest(
        app_dir, image, source_size=(tmp_path / "os.img").stat().st_size + 1
    )
    monkeypatch.setattr(window_mod, "APP_DIR", app_dir)
    w = _make_window(qapp, tmp_path)
    try:
        assert w._resume_banner.isHidden()
        assert w._scan_unfinished_jobs() == []
    finally:
        w._shutdown()
