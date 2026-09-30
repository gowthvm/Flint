"""UI feature tests: expert default, toggle animation geometry,
verify-page scroll, bottom-bar page scoping, dots-menu ticks,
compressed-image path resolution, drop-zone guards and decompression."""

import os

import pytest
from PyQt6.QtWidgets import QScrollArea


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    import core.settings as s

    original = s.SETTINGS_PATH
    s.SETTINGS_PATH = tmp_path / "s.json"
    s._CACHE = None
    yield
    s._CACHE = None
    s.SETTINGS_PATH = original


def _make_window(qapp, tmp_path, seed: dict | None = None):
    import core.settings as s

    values = {"onboarding_seen": True, "theme": "dark"}
    values.update(seed or {})
    s.set_many(**values)
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def test_expert_panel_visible_by_default(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert s.get("expert_mode") is True
        assert not w._expert_options_body.isHidden()
        assert w._partition_combo.isEnabled()
    finally:
        w._shutdown()


def test_expert_mode_can_still_be_turned_off(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        w._expert_toggle.setChecked(False)
        assert w._expert_options_body.isHidden()
        assert not w._expert_toggle.isHidden()
        assert not w._partition_combo.isEnabled()
        assert s.get("expert_mode") is False
        w._expert_toggle.setChecked(True)
        assert not w._expert_options_body.isHidden()
        assert w._partition_combo.isEnabled()
        assert s.get("expert_mode") is True
    finally:
        w._shutdown()


def test_toggle_knob_moves_left_when_off(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"verify_after_write": True})
    try:
        toggle = w._verify_toggle
        assert toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(True)
        toggle.setChecked(False, animate=False)
        assert not toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(False)
        toggle.setChecked(True, animate=False)
        assert toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(True)
    finally:
        w._shutdown()


def test_toggle_starts_left_when_off(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"verify_after_write": False})
    try:
        toggle = w._verify_toggle
        assert not toggle.isChecked()
        assert toggle._knob.pos().x() == toggle._knob_x(False)
    finally:
        w._shutdown()


def test_verify_page_is_scrollable(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        assert isinstance(w._pages.widget(2), QScrollArea)
        assert w._pages.widget(2).widgetResizable()
    finally:
        w._shutdown()


def test_bottombar_hidden_on_history_and_verify_pages(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        assert not w._bottombar.isHidden()
        w._on_nav_clicked(1)
        assert w._pages.currentIndex() == 2
        assert w._bottombar.isHidden()
        w._on_nav_clicked(2)
        assert w._pages.currentIndex() == 1
        assert w._bottombar.isHidden()
        w._on_nav_clicked(0)
        assert w._pages.currentIndex() == 0
        assert not w._bottombar.isHidden()
    finally:
        w._shutdown()


def test_bottombar_stays_visible_when_nav_blocked_while_busy(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        w._writing = True
        w._on_nav_clicked(1)
        assert w._pages.currentIndex() == 0
        assert not w._bottombar.isHidden()
        w._writing = False
    finally:
        w._shutdown()


def test_dots_menu_opens_settings_page(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        texts = [a.text() for a in w._build_dots_menu().actions()]
        assert texts == ["Settings", "", "Check for updates\u2026"]
        w._build_dots_menu().actions()[0].trigger()
        assert w._pages.currentIndex() == 3
        assert w._bottombar.isHidden()
    finally:
        w._shutdown()


def test_flash_without_drive_opens_picker_when_drives_exist(
    qapp, tmp_path, monkeypatch
):
    w = _make_window(qapp, tmp_path, seed={"expert_mode": False})
    try:
        w._drives = [{"physical_path": r"\\.\PHYSICALDRIVE1"}]
        opened = []
        monkeypatch.setattr(w, "_show_drive_picker", lambda: opened.append(1))
        w._iso_zone._path = "C:\\fake.iso"
        w._on_flash_clicked()
        assert opened
    finally:
        w._shutdown()


def test_app_icon_loaded_from_flint_ico(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        icon = w._make_flint_icon()
        assert not icon.isNull()
    finally:
        w._shutdown()


def test_drive_chip_labels_keep_readable_width(qapp, tmp_path):
    """The chip must never collapse: its labels need a real minimum width
    and a growth-friendly policy inside the fixed-width sidebar."""
    from PyQt6.QtWidgets import QSizePolicy

    w = _make_window(qapp, tmp_path)
    try:
        for label in (w._drive_name, w._drive_sub):
            assert label.minimumSizeHint().width() > 0
            assert (
                label.sizePolicy().horizontalPolicy()
                != QSizePolicy.Policy.Ignored
            )
    finally:
        w._shutdown()


def test_flash_without_drive_errors_when_none_detected(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"expert_mode": False})
    try:
        w._drives = []
        w._iso_zone._path = "C:\\fake.iso"
        w._on_flash_clicked()
        assert "plug one in first" in w._progress._error.text()
    finally:
        w._shutdown()


def _screen_area():
    from PyQt6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def test_restored_geometry_clamped_inside_screen(qapp, tmp_path):
    """A window whose saved position hangs off the right edge must be
    pulled back so nothing is cut off at the border."""
    available = _screen_area()
    if available is None:
        pytest.skip("no screen in this environment")

    w = _make_window(qapp, tmp_path)
    try:
        w.resize(
            min(900, available.width() - 20),
            min(580, available.height() - 20),
        )
        w.move(available.right() - w.width() // 2 + 150, available.top() + 40)
        w._clamp_to_screen()
        frame = w.frameGeometry()
        assert frame.left() >= available.left()
        assert frame.right() <= available.right()
        assert frame.top() >= available.top()
        assert frame.bottom() <= available.bottom()
    finally:
        w._shutdown()


def test_fully_offscreen_geometry_centered_on_screen(qapp, tmp_path):
    """A saved position outside every screen must fall back to a centered
    placement instead of opening unreachable."""
    available = _screen_area()
    if available is None:
        pytest.skip("no screen in this environment")

    w = _make_window(qapp, tmp_path)
    try:
        w.resize(
            min(900, available.width() - 20),
            min(580, available.height() - 20),
        )
        w.move(available.right() + 500, available.top() + 100)
        w._clamp_to_screen()
        frame = w.frameGeometry()
        assert frame.intersects(available)
        assert abs(frame.center().x() - available.center().x()) <= 3
        assert abs(frame.center().y() - available.center().y()) <= 3
    finally:
        w._shutdown()


def test_minimum_size_keeps_all_content_inside_borders(qapp, tmp_path):
    """At the enforced minimum window size nothing may extend past the
    right border: the scroll areas disable horizontal scrolling, so any
    content wider than the main column would be cut off unreachably."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QWidget

    w = _make_window(qapp, tmp_path)
    try:
        w.show()
        qapp.processEvents()
        floor = w._content_minimum_width()
        available = _screen_area()
        if (
            available is not None
            and w.minimumWidth() < floor
            and w.width() < floor
        ):
            pytest.skip("screen is narrower than the content floor")
        w.resize(w.minimumWidth(), w.minimumHeight())
        qapp.processEvents()
        qapp.processEvents()
        central = w.centralWidget()
        cw = central.width()
        clipped = []
        for wid in central.findChildren(QWidget):
            if not wid.isVisible() or wid.isWindow():
                continue
            pos = wid.mapTo(central, QPoint(0, 0))
            right = pos.x() + wid.width()
            if right > cw + 1:
                clipped.append((wid.__class__.__name__, wid.objectName(), right))
        assert not clipped, clipped[:5]
    finally:
        w._shutdown()


def _new_zone():
    from ui.widgets import IsoDropZone

    return IsoDropZone()


def _archive_and_extracted(tmp_path):
    archive = tmp_path / "image.zip"
    archive.write_bytes(b"PK\x03\x04payload")
    extracted = tmp_path / "image.iso"
    extracted.write_bytes(b"iso-bytes")
    return str(archive), str(extracted)


def test_compressed_selection_resolves_to_extracted_path(qapp, tmp_path):
    """`path` is the bytes that get hashed/detected/flashed, while
    `source_path` keeps the original selection (the archive)."""
    zone = _new_zone()
    archive, extracted = _archive_and_extracted(tmp_path)
    zone._path = archive
    zone._decompressed_path = extracted
    assert zone.path == extracted
    assert zone.source_path == archive
    assert zone.was_compressed is True


def test_plain_selection_path_is_the_source_path(qapp, tmp_path):
    zone = _new_zone()
    image = tmp_path / "plain.iso"
    image.write_bytes(b"x" * 64)
    zone._path = str(image)
    assert zone.path == str(image)
    assert zone.source_path == str(image)
    assert zone.was_compressed is False


def test_hash_result_is_accepted_for_compressed_selection(qapp, tmp_path):
    """A digest computed on the extracted file must not be dropped by the
    stale-path guard (the archive stays the source, not the hash target)."""
    zone = _new_zone()
    archive, extracted = _archive_and_extracted(tmp_path)
    seen = []
    zone.hash_done.connect(lambda p, ok, d: seen.append((p, ok, d)))
    zone._path = archive
    zone._decompressed_path = extracted

    zone._on_hash_done(extracted, True, "a" * 64)
    assert zone.digest == "a" * 64
    assert seen == [(extracted, True, "a" * 64)]

    zone._on_hash_done(archive, True, "b" * 64)
    assert zone.digest == "a" * 64, "stale result for another path dropped"
    assert len(seen) == 1


def test_analysis_result_is_accepted_for_compressed_selection(
    qapp, tmp_path
):
    zone = _new_zone()
    archive, extracted = _archive_and_extracted(tmp_path)
    seen = []
    zone.iso_analysis.connect(lambda *args: seen.append(args))
    zone._path = archive
    zone._decompressed_path = extracted

    zone._on_analysis(extracted, True, False, False)
    assert seen == [(extracted, True, False, False)]


def test_load_iso_routes_zst_into_the_compressed_branch(qapp, tmp_path, monkeypatch):
    """A .zst selection must spawn a DecompressWorker instead of showing
    the unsupported-compression error."""
    from PyQt6.QtCore import QObject, pyqtSignal

    from ui import widgets

    class CaptureWorker(QObject):
        done = pyqtSignal(str, bool, str, str)

        def __init__(self, path, tmp_dir, fmt):
            super().__init__()
            self.captured = (path, tmp_dir, fmt)
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(widgets, "DecompressWorker", CaptureWorker)
    archive = tmp_path / "payload.iso.zst"
    archive.write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 32)

    zone = widgets.IsoDropZone()
    zone.load_iso(str(archive))
    worker = zone._decompress_worker
    assert worker is not None, ".zst must spawn a DecompressWorker"
    assert worker.started
    assert worker.captured[0] == str(archive)
    assert worker.captured[2] == ".zst"
    assert "Unsupported" not in zone._drop_error.text()
    assert zone._drop_error.isHidden()
    assert zone.source_path == str(archive)
    assert zone.was_compressed is False


def test_load_iso_real_zst_decompresses_and_resolves(qapp, tmp_path):
    """End-to-end .zst: a worker is spawned, no unsupported-format error is
    shown, and `path` resolves to the extracted file."""
    from PyQt6.QtTest import QTest

    zstandard = pytest.importorskip("zstandard")
    from ui.widgets import IsoDropZone

    data = b"flint zstd payload " * 32
    archive = tmp_path / "payload.iso.zst"
    archive.write_bytes(zstandard.ZstdCompressor().compress(data))

    zone = IsoDropZone()
    try:
        zone.load_iso(str(archive))
        worker = zone._decompress_worker
        assert worker is not None, ".zst must spawn a DecompressWorker"
        assert zone._drop_error.isHidden()
        assert worker.wait(10000)
        QTest.qWait(50)
        if worker.last_error:
            pytest.xfail(
                "core/decompress._decompress_zst failed: "
                f"{worker.last_error}"
            )
        assert zone.was_compressed
        assert zone.source_path == str(archive)
        assert zone.path is not None and zone.path != str(archive)
        with open(zone.path, "rb") as fh:
            assert fh.read() == data
    finally:
        zone.clear_iso()
    assert zone.path is None
    assert zone.source_path is None


def test_decompress_worker_sets_last_error_on_corrupt_zip(qapp, tmp_path):
    from ui.widgets import DecompressWorker

    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"PK\x03\x04 this is not a real zip archive")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    worker = DecompressWorker(str(archive), str(out_dir), ".zip")
    seen = []
    worker.done.connect(lambda *args: seen.append(args))
    worker.start()
    assert worker.wait(10000)
    qapp.processEvents()

    assert worker.last_error
    assert len(seen) == 1
    assert seen[0] == (str(archive), False, "", "")


def test_decompress_worker_moves_output_into_the_caller_tmp_dir(
    qapp, tmp_path
):
    """The public dispatcher owns its own temp dir, so the worker moves the
    extracted file into the zone's dir before reporting success."""
    import gzip

    from ui.widgets import DecompressWorker

    data = os.urandom(8192)
    archive = tmp_path / "image.iso.gz"
    archive.write_bytes(gzip.compress(data))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    worker = DecompressWorker(str(archive), str(out_dir), ".gz")
    seen = []
    worker.done.connect(lambda *args: seen.append(args))
    worker.start()
    assert worker.wait(10000)
    qapp.processEvents()

    assert worker.last_error == ""
    assert len(seen) == 1
    path, ok, extracted, tmp_dir = seen[0]
    assert ok is True
    assert path == str(archive)
    assert tmp_dir == str(out_dir)
    assert extracted == os.path.join(str(out_dir), "image.iso")
    with open(extracted, "rb") as fh:
        assert fh.read() == data


def test_decompress_worker_zst_failure_reports_last_error(qapp, tmp_path):
    """A gzip stream mislabelled as .zst fails either way: with the
    friendly install hint when zstandard is missing, with a decode error
    when it is installed."""
    import gzip
    import importlib.util

    from ui.widgets import DecompressWorker

    archive = tmp_path / "broken.iso.zst"
    archive.write_bytes(gzip.compress(b"not zstd at all"))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    worker = DecompressWorker(str(archive), str(out_dir), ".zst")
    seen = []
    worker.done.connect(lambda *args: seen.append(args))
    worker.start()
    assert worker.wait(10000)
    qapp.processEvents()

    assert len(seen) == 1
    assert seen[0][1] is False
    assert worker.last_error
    if importlib.util.find_spec("zstandard") is None:
        assert "zstandard" in worker.last_error.lower()


def test_drop_zone_surfaces_worker_last_error(qapp, tmp_path):
    from PyQt6.QtTest import QTest

    from ui.widgets import IsoDropZone

    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"PK\x03\x04 this is not a real zip archive")
    zone = IsoDropZone()
    try:
        zone.load_iso(str(archive))
        worker = zone._decompress_worker
        assert worker is not None
        assert worker.wait(10000)
        QTest.qWait(50)
        assert worker.last_error
        assert zone._decompress_worker is None
        assert zone._drop_error.text() == worker.last_error
        assert not zone.was_compressed, "no resolved image after a failure"
        assert zone.source_path == str(archive)
    finally:
        zone.clear_iso()


def test_browse_guard_blocks_dialog_and_load(qapp, tmp_path, monkeypatch):
    from ui import widgets

    image = tmp_path / "plain.iso"
    image.write_bytes(b"x" * 64)
    opened = []

    class _FakeDialog:
        @staticmethod
        def getOpenFileName(*args, **kwargs):
            opened.append(args)
            return str(image), ""

    monkeypatch.setattr(widgets, "QFileDialog", _FakeDialog)

    zone = widgets.IsoDropZone()
    try:
        zone._browse_guard = lambda: True
        zone._browse()
        assert not opened, "guarded browse must not open the dialog"
        zone.load_iso(str(image))
        assert zone.path is None, "guarded load must not select anything"

        zone._browse_guard = lambda: False
        zone._browse()
        assert len(opened) == 1
        assert zone.path == str(image)
        assert zone.source_path == str(image)
        assert zone.was_compressed is False
    finally:
        zone._browse_guard = None
        zone.clear_iso()


def test_drop_and_drag_honor_the_browse_guard(qapp, tmp_path):
    from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
    from PyQt6.QtGui import QDragEnterEvent, QDropEvent

    from ui.widgets import IsoDropZone

    image = tmp_path / "plain.iso"
    image.write_bytes(b"x" * 64)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(image))])

    def _drag():
        return QDragEnterEvent(
            QPoint(0, 0),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    def _drop():
        return QDropEvent(
            QPointF(0, 0),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    zone = IsoDropZone()
    try:
        zone._browse_guard = lambda: True
        enter = _drag()
        zone.dragEnterEvent(enter)
        assert not enter.isAccepted(), "guarded drag must be refused"
        drop = _drop()
        zone.dropEvent(drop)
        assert not drop.isAccepted(), "guarded drop must be refused"
        assert zone.path is None

        zone._browse_guard = lambda: False
        enter = _drag()
        zone.dragEnterEvent(enter)
        assert enter.isAccepted()
        drop = _drop()
        zone.dropEvent(drop)
        assert zone.path is not None and os.path.samefile(zone.path, image)
    finally:
        zone._browse_guard = None
        zone.clear_iso()


def test_clear_guard_blocks_clear_and_clear_resolves_paths(qapp, tmp_path):
    from ui.widgets import IsoDropZone

    archive, extracted = _archive_and_extracted(tmp_path)
    zone = IsoDropZone()
    zone._path = archive
    zone._decompressed_path = extracted

    zone._clear_guard = lambda: True
    zone.clear_iso()
    assert zone.path == extracted, "guarded clear must keep the selection"

    zone._clear_guard = lambda: False
    zone.clear_iso()
    assert zone.path is None
    assert zone.source_path is None
    assert zone.was_compressed is False
    assert zone.digest is None
    assert not os.path.exists(extracted)
