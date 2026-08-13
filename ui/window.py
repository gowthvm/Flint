import ctypes
from ctypes import wintypes
import hashlib
import os
import shutil
import sys
import time
import logging

from PyQt6.QtCore import (
    QByteArray,
    QEvent,
    QPointF,
    Qt,
    QThread,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QCheckBox,
    QWidget,
)

from ui import style as style

from core import settings
from core.bootcheck import probe_bootability
from core.drives import DriveDetector, DrivePoller
from core.eject import eject_drive
from core.history import (
    append_history,
    clear_history,
    export_history,
    flash_report,
    import_history,
    load_history,
)
from core.verify import VerifyWorker
from core.wipe import WipeWorker
from core.writer import UsbWriter

logger = logging.getLogger("flint")


class IsoWorker(QThread):
    hash_done = pyqtSignal(str, bool, str)
    progress = pyqtSignal(int)

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def run(self) -> None:
        try:
            digest = hashlib.sha256()
            total = os.path.getsize(self._path)
            read_bytes = 0
            with open(self._path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    if self.isInterruptionRequested():
                        return
                    digest.update(chunk)
                    read_bytes += len(chunk)
                    if total > 0:
                        self.progress.emit(
                            round(read_bytes * 100 / total)
                        )
            if not self.isInterruptionRequested():
                self.hash_done.emit(self._path, True, digest.hexdigest())
        except Exception as exc:
            logger.exception("IsoWorker.run failed")
            if not self.isInterruptionRequested():
                self.hash_done.emit(self._path, False, "")


class IsoDropZone(QFrame):
    iso_selected = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("isoDropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._path: str | None = None
        self._worker: IsoWorker | None = None
        self._digest: str | None = None
        self._hash_finished = False
        self._retired_workers: list[QThread] = []
        self._clear_guard = None
        self.setToolTip("Drop an ISO or click to browse (Ctrl+O)")

        self._empty = self._build_empty_state()
        self._loaded = self._build_loaded_state()
        self._loaded.setVisible(False)

        self._drop_error = QLabel(
            "Only .iso, .img or .bin images are supported"
        )
        self._drop_error.setObjectName("dropError")
        self._drop_error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_error.setVisible(False)
        self._drop_timer = QTimer(self)
        self._drop_timer.setSingleShot(True)
        self._drop_timer.timeout.connect(
            lambda: self._drop_error.setVisible(False)
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._empty)
        layout.addWidget(self._loaded)
        layout.addWidget(self._drop_error)
        layout.addSpacing(8)

    def _build_empty_state(self) -> QWidget:
        widget = QWidget()
        col = QVBoxLayout(widget)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        icon = QLabel("\u25a4")
        icon.setObjectName("emptyIsoIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel("Drop an ISO or click to browse")
        text.setObjectName("emptyIsoText")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addStretch()
        col.addWidget(icon)
        col.addWidget(text)
        col.addStretch()
        return widget

    def _build_loaded_state(self) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)

        icon = QLabel("\u25a4")
        icon.setObjectName("isoIcon")
        icon.setFixedSize(
            style.DESIGN_TOKENS["icon_large"],
            style.DESIGN_TOKENS["icon_large"],
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        info = QVBoxLayout()
        info.setSpacing(3)
        self._iso_name = QLabel("")
        self._iso_name.setObjectName("isoName")
        self._iso_meta = QLabel("")
        self._iso_meta.setObjectName("isoMeta")
        info.addWidget(self._iso_name)
        info.addWidget(self._iso_meta)

        self._iso_check = QLabel("\u2713")
        self._iso_check.setObjectName("isoCheck")
        self._iso_check.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._iso_clear = QPushButton("\u2715")
        self._iso_clear.setObjectName("isoClear")
        self._iso_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._iso_clear.setFixedSize(
            style.DESIGN_TOKENS["icon_medium"],
            style.DESIGN_TOKENS["icon_medium"],
        )
        self._iso_clear.setToolTip("Remove image")
        self._iso_clear.clicked.connect(self.clear_iso)

        row.addWidget(icon)
        row.addLayout(info)
        row.addStretch()
        row.addWidget(self._iso_clear)
        row.addWidget(self._iso_check)
        return widget

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._browse()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self._browse()
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if not urls:
            event.ignore()
            return
        url = self._first_iso_url(event)
        if url:
            self._drop_error.setVisible(False)
            self._drop_timer.stop()
            self.load_iso(url.toLocalFile())
        else:
            self._drop_error.setVisible(True)
            self._drop_timer.start(3600)
            event.acceptProposedAction()

    def _first_iso_url(self, event):
        urls = event.mimeData().urls()
        for url in urls:
            if (
                url.isLocalFile()
                and url.toLocalFile().lower().endswith(
                    (".iso", ".img", ".bin")
                )
            ):
                return url
        return None

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select image",
            settings.get("last_iso_dir") or "",
            "Disk image (*.iso *.img *.bin);;All files (*)",
        )
        if path:
            settings.set_many(last_iso_dir=os.path.dirname(path))
            self.load_iso(path)

    def clear_iso(self) -> None:
        if self._clear_guard is not None and self._clear_guard():
            return
        old = self._worker
        if old is not None and old.isRunning():
            old.requestInterruption()
            old.wait(3000)
        if old is not None:
            self._retired_workers.append(old)
        self._worker = None
        self._digest = None
        self._hash_finished = False
        self._path = None
        self._drop_error.setVisible(False)
        self._drop_timer.stop()
        self._loaded.setVisible(False)
        self._empty.setVisible(True)
        self.setProperty("loaded", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def load_iso(self, path: str) -> None:
        if not path or not os.path.isfile(path):
            return
        self._drop_error.setVisible(False)
        self._drop_timer.stop()
        old = self._worker
        if old is not None and old.isRunning():
            old.requestInterruption()
            old.wait(3000)
        if old is not None:
            self._retired_workers.append(old)
        self._worker = None
        self._digest = None
        self._hash_finished = False
        self._path = path
        size = DriveDetector.format_size(os.path.getsize(path))
        self._iso_name.setText(os.path.basename(path))
        self._set_meta(False, f"{size} \u00b7 Verifying\u2026")
        self._loaded.setVisible(True)
        self._empty.setVisible(False)
        self.setProperty("loaded", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.iso_selected.emit(path)

        worker = IsoWorker(path)
        self._worker = worker
        worker.hash_done.connect(self._on_hash_done)
        worker.progress.connect(self._on_hash_progress)
        worker.start()

    def _on_hash_progress(self, percent: int) -> None:
        if self._path is None or self._hash_finished:
            return
        size = DriveDetector.format_size(
            os.path.getsize(self._path) if os.path.isfile(self._path) else 0
        )
        self._set_meta(False, f"{size} \u00b7 Reading image\u2026 {percent}%")

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def digest(self) -> str | None:
        return self._digest

    def _on_hash_done(self, path: str, ok: bool, digest: str) -> None:
        if path != self._path:
            return
        self._hash_finished = True
        self._digest = digest if ok else None
        size = DriveDetector.format_size(os.path.getsize(self._path or ""))
        if ok:
            self._set_meta(True, f"{size} \u00b7 SHA256 verified")
        else:
            self._set_meta(False, f"{size} \u00b7 SHA256 failed")

    def _set_meta(self, ok: bool, text: str) -> None:
        self._iso_meta.setText(text)
        self._iso_meta.setProperty("error", not ok)
        self._iso_meta.style().unpolish(self._iso_meta)
        self._iso_meta.style().polish(self._iso_meta)


class ShaInput(QLineEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("shaInput")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasText() or any(
            url.isLocalFile() for url in mime.urls()
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if urls:
            for url in urls:
                try:
                    with open(
                        url.toLocalFile(),
                        "r",
                        encoding="utf-8",
                        errors="ignore",
                    ) as f:
                        text = f.read(256).strip().lower()
                except OSError:
                    continue
                if (
                    len(text) == 64
                    and all(
                        c in "0123456789abcdef" for c in text
                    )
                ):
                    self.setText(text)
                    event.acceptProposedAction()
                    return
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class SegmentedControl(QWidget):
    valueChanged = pyqtSignal(str)

    def __init__(self, options: list[str], default_index: int = 0) -> None:
        super().__init__()
        self._options = list(options)
        self._value = self._options[default_index]
        self._active = default_index
        self._buttons: list[QPushButton] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for index, text in enumerate(self._options):
            button = QPushButton(text)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            button.clicked.connect(lambda _, i=index: self._select(i))
            layout.addWidget(button)
            self._buttons.append(button)
        self._apply()

    def _apply(self) -> None:
        for i, button in enumerate(self._buttons):
            button.setObjectName("segOn" if i == self._active else "seg")
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def _select(self, index: int) -> None:
        if index == self._active:
            return
        self._active = index
        self._value = self._options[index]
        self._apply()
        self.valueChanged.emit(self._value)

    @pyqtProperty(str, notify=valueChanged)
    def value(self) -> str:
        return self._value


class ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True) -> None:
        super().__init__()
        self._checked = checked
        self.setFixedSize(
            style.DESIGN_TOKENS["toggle_w"],
            style.DESIGN_TOKENS["toggle_h"],
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Verify after write")
        self.setAccessibleDescription(
            "Check the drive after writing by comparing its content "
            "against the image"
        )

        self._track = QLabel()
        self._track.setObjectName("toggleTrack")
        self._track.setFixedSize(
            style.DESIGN_TOKENS["toggle_w"],
            style.DESIGN_TOKENS["toggle_h"],
        )
        layout = QHBoxLayout(self._track)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        self._knob = QLabel()
        self._knob.setObjectName("toggleKnob")
        self._knob.setFixedSize(
            style.DESIGN_TOKENS["toggle_knob"],
            style.DESIGN_TOKENS["toggle_knob"],
        )
        layout.addStretch(1)
        layout.addWidget(self._knob)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._track)
        self._apply()

    def _apply(self) -> None:
        self._track.setProperty("on", self._checked)
        self._track.style().unpolish(self._track)
        self._track.style().polish(self._track)
        self._knob.style().unpolish(self._knob)
        self._knob.style().polish(self._knob)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._apply()
        self.toggled.emit(checked)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.setChecked(not self._checked)
        else:
            super().keyPressEvent(event)


class ProgressArea(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("progressArea")
        self._total = 0
        self._written = 0

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        head = QHBoxLayout()
        self._title = QLabel("Writing\u2026")
        self._title.setObjectName("progTitle")
        self._pct = QLabel("0%")
        self._pct.setObjectName("progPct")
        head.addWidget(self._title)
        head.addStretch()
        head.addWidget(self._pct)

        self._bar = QProgressBar()
        self._bar.setObjectName("progressBar")
        self._bar.setValue(0)
        self._bar.setTextVisible(False)

        self._written_stat = self._make_stat("Written")
        self._speed_stat = self._make_stat("Speed")
        self._eta_stat = self._make_stat("Remaining")
        stats = QHBoxLayout()
        stats.setSpacing(10)
        stats.addLayout(self._written_stat)
        stats.addLayout(self._speed_stat)
        stats.addLayout(self._eta_stat)
        stats.addStretch()

        self._error = QLabel("")
        self._error.setObjectName("progError")
        self._error.setWordWrap(True)
        self._error.setVisible(False)

        col.addLayout(head)
        col.addSpacing(14)
        col.addWidget(self._bar)
        col.addSpacing(10)
        col.addLayout(stats)
        col.addWidget(self._error)

    def _make_stat(self, cap: str) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(2)
        cap_label = QLabel(cap)
        cap_label.setObjectName("statCap")
        self._stat_values: dict[str, QLabel] = getattr(
            self, "_stat_values", {}
        )
        value_label = QLabel("0.0 GB / 0.0 GB" if cap == "Written" else "0")
        value_label.setObjectName("statVal")
        self._stat_values[cap] = value_label
        col.addWidget(cap_label)
        col.addWidget(value_label)
        return col

    def _stat_text(self, cap: str) -> str:
        return self._stat_values[cap].text()

    def set_ready(self) -> None:
        self._total = 0
        self._written = 0
        self._title.setText("Ready")
        self._pct.setText("\u2014")
        self._bar.setValue(0)
        self._stat_values["Written"].setText("\u2014")
        self._stat_values["Speed"].setText("\u2014")
        self._stat_values["Remaining"].setText("\u2014")
        self._error.setVisible(False)

    def reset(self) -> None:
        self._total = 0
        self._written = 0
        self._title.setText("Writing\u2026")
        self._pct.setText("0%")
        self._bar.setValue(0)
        self.set_values(0, 0.0, 0)
        self._error.setVisible(False)

    def set_progress(self, percent: float) -> None:
        self._pct.setText(f"{percent:.0f}%")
        self._bar.setValue(round(percent))

    def set_speed(self, mbps: float) -> None:
        self._stat_values["Speed"].setText(f"{mbps:.0f} MB/s")

    def set_total(self, total: int) -> None:
        self._total = total
        self._stat_values["Written"].setText(
            f"{self._fmt_gb(self._written)} / {self._fmt_gb(total)}"
        )

    def set_written(self, written: int) -> None:
        self._written = written
        self._stat_values["Written"].setText(
            f"{self._fmt_gb(written)} / {self._fmt_gb(self._total)}"
        )

    def set_eta(self, seconds: int) -> None:
        self._stat_values["Remaining"].setText(f"~{seconds} s")

    def set_values(self, written: int, mbps: float, seconds: int) -> None:
        self.set_written(written)
        self.set_speed(mbps)
        self.set_eta(seconds)

    def set_verifying(self) -> None:
        self._title.setText("Verifying\u2026")
        self._pct.setText("0%")
        self._bar.setValue(0)
        self._stat_values["Written"].setText("\u2014")
        self._stat_values["Speed"].setText("Reading\u2026")
        self._stat_values["Remaining"].setText("\u2014")
        self._error.setVisible(False)

    def set_stats_written(self, written: int, total: int) -> None:
        if total > 0:
            self._total = total
            self._written = written
            self._stat_values["Written"].setText(
                f"{self._fmt_gb(written)} / {self._fmt_gb(total)}"
            )

    def set_phase(self, phase: str) -> None:
        self._title.setText(f"{phase}\u2026")

    def set_done(self) -> None:
        self._title.setText("Done")
        self._pct.setText("100%")
        self._bar.setValue(100)

    def set_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setProperty("error", True)
        self._error.style().unpolish(self._error)
        self._error.style().polish(self._error)
        self._error.setVisible(True)

    def set_warning(self, message: str) -> None:
        self._error.setText(message)
        self._error.setProperty("error", False)
        self._error.style().unpolish(self._error)
        self._error.style().polish(self._error)
        self._error.setVisible(True)

    @staticmethod
    def _fmt_gb(num_bytes: int) -> str:
        return f"{num_bytes / 1_000_000_000:.1f} GB"


class DriveChip(QFrame):
    clicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("driveChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)


class NavItem(QFrame):
    clicked = pyqtSignal()

    def __init__(
        self,
        glyph: str,
        text: str,
        active: bool,
        badge: str | None = None,
    ) -> None:
        super().__init__()
        self._glyph = glyph
        self._text = text
        self._badge = badge
        self.setObjectName("navItem")
        self.setProperty("on", active)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(9)

        self._icon = QLabel(glyph)
        self._icon.setObjectName("navIcon")
        self._icon.setProperty("on", active)
        self._icon.setFixedWidth(16)

        label = QLabel(text)
        label.setObjectName("navText")
        label.setProperty("on", active)

        row.addWidget(self._icon)
        row.addWidget(label)
        row.addStretch()
        if badge is not None:
            self._badge_label = QLabel(badge)
            self._badge_label.setObjectName("badgeOn" if active else "badge")
            self._badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(self._badge_label)
        else:
            self._badge_label = None

    def set_active(self, active: bool) -> None:
        self.setProperty("on", active)
        for widget in (self, self._icon):
            widget.setProperty("on", active)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()
        if self._badge_label is not None:
            self._badge_label.setObjectName(
                "badgeOn" if active else "badge"
            )
            self._badge_label.style().unpolish(self._badge_label)
            self._badge_label.style().polish(self._badge_label)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Flint")
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.resize(
                min(900, max(640, geo.width() - 80)),
                min(580, max(440, geo.height() - 100)),
            )
            self.setMinimumSize(
                min(860, max(640, geo.width() - 80)),
                min(540, max(420, geo.height() - 100)),
            )
        else:
            self.resize(900, 580)
            self.setMinimumSize(860, 540)

        self._detector = DriveDetector()
        self._current_drive: dict | None = None
        self._active_write_drive: dict | None = None
        self._drives: list[dict] = []
        self._writer: UsbWriter | None = None
        self._verifier: VerifyWorker | None = None
        self._page_verifier: VerifyWorker | None = None
        self._wipe_worker: WipeWorker | None = None
        self._retired_workers: list[QThread] = []
        self._last_report: dict | None = None
        self._controls: list[QWidget] = []
        self._writing = False
        self._write_started = 0.0
        self._tray: QSystemTrayIcon | None = None
        self._tb = None
        self._tb_tried = False

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = self._build_sidebar()
        divider = QFrame()
        divider.setObjectName("vdiv")
        divider.setFixedWidth(1)

        root.addWidget(sidebar)
        root.addWidget(divider)
        root.addWidget(self._build_main())

        self.setCentralWidget(central)

        self._controls = [
            self._iso_zone,
            self._verify_toggle,
            self._cancel_btn,
            self._refresh_btn,
            self._dots_btn,
        ]

        # Keep primary action states in sync with selections and busy state
        try:
            self._iso_zone.iso_selected.connect(self._on_iso_selected)
        except Exception:
            pass
        self._update_controls_state()

        geometry = settings.get("window_geometry")
        if geometry:
            self.restoreGeometry(
                QByteArray.fromBase64(geometry.encode("ascii"))
            )

        self._poller = DrivePoller(self._detector, 2000)
        self._poller.drives_ready.connect(self._on_drives_ready)
        self._poller.start()
        self._cancel_btn.setEnabled(False)
        self._wipe_btn.setEnabled(False)
        self._verify_toggle.setChecked(
            bool(settings.get("verify_after_write"))
        )
        self._dots_btn.clicked.connect(self._show_dots_menu)
        self._iso_zone._clear_guard = lambda: self._busy()
        self._verify_zone._clear_guard = lambda: self._busy()
        QShortcut(QKeySequence("F5"), self).activated.connect(
            self._request_scan
        )
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(
            self._iso_zone._browse
        )
        for s in self.findChildren(QShortcut):
            s.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.setWindowIcon(self._make_flint_icon())
        self._setup_tray()

        # One-time onboarding modal to improve discoverability
        try:
            if not settings.get("onboarding_seen"):
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Information)
                box.setWindowTitle("Welcome to Flint")
                box.setText(
                    "Welcome — a quick tour:\n\n"
                    "1) Pick an image\n"
                    "2) Choose a target drive\n"
                    "3) Flash and optionally verify\n\n"
                    "Dangerous actions (wipe/flash) require typed confirmation."
                )
                cb = QCheckBox("Don't show this again")
                box.setCheckBox(cb)
                box.addButton("Close", QMessageBox.ButtonRole.AcceptRole)
                box.exec()
                settings.set_many(onboarding_seen=True)
        except Exception:
            pass

    def _busy(self) -> bool:
        return bool(
            self._writing
            or self._writer is not None
            or self._verifier is not None
            or self._page_verifier is not None
            or self._wipe_worker is not None
        )

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(200)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        logo_row = QHBoxLayout()
        logo_row.setContentsMargins(20, 22, 20, 22)
        logo_row.setSpacing(10)
        mark = QLabel("\u25c6")
        mark.setObjectName("logoMark")
        mark.setFixedSize(26, 26)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name = QLabel("Flint")
        name.setObjectName("logoName")
        logo_row.addWidget(mark)
        logo_row.addWidget(name)
        logo_row.addStretch()

        logo_box = QWidget()
        logo_box.setLayout(logo_row)

        logo_divider = QFrame()
        logo_divider.setObjectName("hdiv")
        logo_divider.setFixedHeight(1)

        nav_items = [
            ("\u270e", "Write", True, None),
            ("\u2714", "Verify", False, None),
            ("\u21ba", "History", False, None),
        ]

        nav = QVBoxLayout()
        nav.setContentsMargins(8, 12, 8, 12)
        nav.setSpacing(1)
        self._nav_items: list[NavItem] = []
        for index, (glyph, text, active, badge) in enumerate(nav_items):
            item = NavItem(glyph, text, active, badge)
            item.clicked.connect(lambda i=index: self._on_nav_clicked(i))
            self._nav_items.append(item)
            nav.addWidget(item)
        nav.addStretch()

        nav_box = QWidget()
        nav_box.setLayout(nav)

        foot_divider = QFrame()
        foot_divider.setObjectName("hdiv")
        foot_divider.setFixedHeight(1)

        foot = QHBoxLayout()
        foot.setContentsMargins(8, 12, 8, 12)
        chip = self._build_drive_chip()
        foot.addWidget(chip)

        foot_box = QWidget()
        foot_box.setLayout(foot)

        layout.addWidget(logo_box)
        layout.addWidget(logo_divider)
        layout.addWidget(nav_box, 1)
        layout.addWidget(foot_divider)
        layout.addWidget(foot_box)
        return sidebar

    def _build_drive_chip(self) -> QFrame:
        chip = DriveChip()
        chip.clicked.connect(self._show_drive_picker)
        self._chip = chip
        row = QHBoxLayout(chip)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(8)

        self._chip_dot = QLabel()
        self._chip_dot.setObjectName("dot")
        self._chip_dot.setFixedSize(6, 6)

        info = QVBoxLayout()
        info.setSpacing(1)
        self._drive_name = QLabel("No drive detected")
        self._drive_name.setObjectName("driveName")
        self._drive_sub = QLabel("")
        self._drive_sub.setObjectName("driveSub")
        info.addWidget(self._drive_name)
        info.addWidget(self._drive_sub)

        row.addWidget(self._chip_dot, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(info)
        row.addStretch()
        return chip

    def _show_drive_picker(self) -> None:
        if self._busy():
            return
        menu = QMenu(self)
        if not self._drives:
            action = menu.addAction("No USB drive detected")
            action.setEnabled(False)
        else:
            selected_path = (
                self._current_drive or {}
            ).get("physical_path")
            for drive in self._drives:
                size = DriveDetector.format_size(
                    drive["size_gb"] * 1_000_000_000
                )
                serial = self._serial_tail(drive)
                label = (
                    f"{drive['model'] or drive['name']} \u00b7 "
                    f"{size} \u00b7 {drive['letter']}:"
                )
                if serial:
                    label += f" \u00b7 S/N \u2026{serial}"
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(
                    drive.get("physical_path") == selected_path
                )
                action.triggered.connect(
                    lambda _, d=drive: self._select_drive(d)
                )
        menu.addSeparator()
        refresh = menu.addAction("\u21bb Refresh")
        refresh.triggered.connect(self._request_scan)
        menu.exec(QCursor.pos())

    def _select_drive(self, drive: dict) -> None:
        if self._busy():
            return
        self._current_drive = drive
        self._update_drive_ui()
        # refresh primary control state when drives change
        try:
            self._update_controls_state()
        except Exception:
            pass

    def _update_drive_ui(self) -> None:
        drive = self._current_drive
        self._wipe_btn.setEnabled(drive is not None)
        self._done_bar.setVisible(False)
        if drive is None:
            self._chip_dot.setProperty("dim", True)
            self._drive_name.setProperty("dim", True)
            self._drive_sub.setProperty("dim", True)
            self._target_detail.setProperty("dim", True)
            if not self._drives:
                if self._detector.last_error:
                    self._drive_name.setText("Drive detection failed")
                    self._drive_sub.setText("Re-run as administrator")
                    self._target_change.setVisible(
                        self._is_elevated()
                    )
                    self._target_admin_btn.setVisible(
                        not self._is_elevated()
                    )
                else:
                    self._drive_name.setText("No drive detected")
                    self._drive_sub.setText("")
                    self._target_change.setVisible(True)
                    self._target_admin_btn.setVisible(False)
                self._target_detail.setText(
                    "No drive detected \u2014 plug in a USB drive"
                )
            else:
                self._drive_name.setText("Choose a drive")
                self._drive_sub.setText(
                    f"{len(self._drives)} drive"
                    f"{'s' if len(self._drives) > 1 else ''} available"
                )
                self._target_detail.setText(
                    "Choose a drive \u2014 "
                    f"{len(self._drives)} available"
                )
                self._target_change.setVisible(True)
                self._target_admin_btn.setVisible(False)
            self._subtitle.setText(
                "No drive selected \u2014 click the drive card"
            )
            if hasattr(self, "_verify_target"):
                self._verify_target.setText("Target: no drive selected")
        else:
            name = drive["model"] or drive["name"]
            size = DriveDetector.format_size(
                drive["size_gb"] * 1_000_000_000
            )
            serial = self._serial_tail(drive)
            self._chip_dot.setProperty("dim", False)
            self._drive_name.setProperty("dim", False)
            self._drive_sub.setProperty("dim", False)
            self._target_detail.setProperty("dim", False)
            self._drive_name.setText(name)
            self._drive_sub.setText(
                f"{size} \u00b7 {drive['bus_type']} \u00b7 "
                f"{drive['letter']}:"
            )
            detail = f"{name} \u00b7 {size} \u00b7 {drive['letter']}:"
            if serial:
                detail += f" \u00b7 S/N \u2026{serial}"
            self._target_detail.setText(detail)
            self._target_change.setVisible(True)
            self._target_admin_btn.setVisible(False)
            self._subtitle.setText(f"{name} \u00b7 {size} selected")
            if hasattr(self, "_verify_target"):
                self._verify_target.setText(
                    f"Target: {name} \u00b7 {drive['letter']}:"
                )
        for widget in (
            self._chip_dot,
            self._drive_name,
            self._drive_sub,
            self._target_detail,
        ):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    @staticmethod
    def _serial_tail(drive: dict) -> str | None:
        serial = (drive.get("serial") or "").strip()
        if len(serial) >= 4:
            return serial[-4:]
        return serial or None

    @staticmethod
    def _is_elevated() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception as exc:
            logger.exception("_is_elevated check failed")
            return False

    @staticmethod
    def _elevation_command() -> tuple[str, str]:
        if getattr(sys, "frozen", False):
            return sys.executable, ""
        script = os.path.abspath(sys.argv[0])
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            alt = exe[: -len("python.exe")] + "pythonw.exe"
            if os.path.isfile(alt):
                exe = alt
        return exe, f'"{script}"'

    def _relaunch_elevated(self) -> None:
        if self._is_elevated():
            return
        cmd, args = self._elevation_command()
        if not cmd:
            return
        # Show an explicit pre-elevation dialog so the user isn't surprised.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Flint — elevation required")
        box.setText(
            "Flint needs administrator privileges to access raw disks.\n\n"
            "Elevating will restart the application with elevated rights."
        )
        elevate_btn = box.addButton("Elevate", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Continue without elevation", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not elevate_btn:
            logger.info("user chose to continue without elevation from UI")
            return
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", cmd, args, None, 5
            )
        except Exception as exc:
            logger.exception("ShellExecuteW failed during relaunch")
            result = 0
        if result <= 32:
            self._progress.set_error(
                "Windows declined to run Flint elevated"
            )
            return
        self._save_settings()
        self._shutdown()
        QApplication.instance().quit()

    def _drive_content_summary(self, drive: dict | None) -> str | None:
        if not drive:
            return None
        letters = drive.get("letters") or (
            [drive["letter"]] if drive.get("letter") else []
        )
        used_total = 0
        count = 0
        for letter in letters:
            try:
                usage = shutil.disk_usage(f"{letter}:\\")
            except OSError:
                continue
            used_total += usage.used
            count += 1
        if count == 0 or used_total <= 0:
            return None
        size = DriveDetector.format_size(used_total)
        plural = "s" if count > 1 else ""
        return (
            f"It holds about {size} of data across {count} "
            f"partition{plural} \u2014 all of it will be erased."
        )

    def _confirm_text(
        self, drive: dict | None, iso_path: str | None = None
    ) -> str:
        if not drive:
            return ""
        name = drive["model"] or drive["name"]
        serial = self._serial_tail(drive)
        drive_size = DriveDetector.format_size(
            (drive.get("size_gb") or 0) * 1_000_000_000
        )
        identity = f"Drive: {name} \u00b7 {drive_size}"
        if serial:
            identity += f" \u00b7 serial \u2026{serial}"
        lines = [identity]
        if iso_path:
            iso_name = os.path.basename(iso_path)
            iso_size = DriveDetector.format_size(os.path.getsize(iso_path))
            lines.append(f"Image: {iso_name} ({iso_size})")
        content = self._drive_content_summary(drive)
        if content:
            lines.append(content)
        lines.append("")
        if iso_path:
            lines.append(
                "Every byte on the drive is permanently replaced. "
                "This cannot be undone."
            )
        else:
            lines.append(
                "The entire drive will be erased, including all "
                "partitions and files. This cannot be undone."
            )
        return "\n".join(lines)

    def _require_typed_confirmation(
        self, drive: dict | None, iso_path: str | None = None
    ) -> bool:
        """Require the user to type a short confirmation string before destructive ops.

        If the drive has a serial number, require the last 4 characters of the
        serial. Otherwise require the user to type the drive model/name.
        Returns True if confirmed, False otherwise.
        """
        if not drive:
            return False
        serial = (drive.get("serial") or "").strip()
        if serial and len(serial) >= 4:
            want = serial[-4:]
            prompt = f"Type the last 4 characters of the drive serial to confirm: …{want}"
        else:
            want = (drive.get("model") or drive.get("name") or "").strip()
            if not want:
                return False
            prompt = f"Type the drive model/name to confirm: {want}"

        from PyQt6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(self, "Confirm destructive action", prompt)
        if not ok:
            return False
        entered = str(text).strip()
        # match case-insensitively for names; exact match for serial tail
        if serial and len(serial) >= 4:
            return entered == want
        return entered.lower() == want.lower()

    def _on_drives_ready(self, drives: list) -> None:
        if self._busy():
            return
        self._drives = drives
        if self._current_drive is not None:
            selected = next(
                (
                    d
                    for d in drives
                    if d.get("physical_path")
                    == self._current_drive.get("physical_path")
                ),
                None,
            )
            if selected is None:
                self._current_drive = None
        self._update_drive_ui()

    def _request_scan(self) -> None:
        self._poller.request_scan()

    def _on_iso_selected(self, path: str) -> None:
        # Called when an ISO is selected; refresh control states
        try:
            self._update_controls_state()
        except Exception:
            pass

    def _build_main(self) -> QWidget:
        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_topbar())

        content_divider = QFrame()
        content_divider.setObjectName("hdiv")
        content_divider.setFixedHeight(1)
        layout.addWidget(content_divider)

        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_content())
        self._pages.addWidget(self._build_history_page())
        self._pages.addWidget(self._build_verify_page())
        layout.addWidget(self._pages, 1)

        bottom_divider = QFrame()
        bottom_divider.setObjectName("hdiv")
        bottom_divider.setFixedHeight(1)
        layout.addWidget(bottom_divider)

        layout.addWidget(self._build_bottombar())
        return main

    def _show_dots_menu(self) -> None:
        menu = QMenu(self)
        theme = settings.get("theme")
        light = menu.addAction("Light theme")
        light.setCheckable(True)
        light.setChecked(theme == "light")
        light.triggered.connect(lambda: self._set_theme("light"))
        contrast = menu.addAction("High contrast")
        contrast.setCheckable(True)
        contrast.setChecked(theme == "high-contrast")
        contrast.triggered.connect(lambda: self._set_theme("high-contrast"))
        dark = menu.addAction("Dark theme")
        dark.setCheckable(True)
        dark.setChecked(theme == "dark")
        dark.triggered.connect(lambda: self._set_theme("dark"))
        menu.addSeparator()
        menu.addAction("Reset window size").triggered.connect(
            lambda: self.resize(900, 580)
        )
        menu.exec(QCursor.pos())

    def _set_theme(self, theme: str) -> None:
        from ui.style import build_style

        QApplication.instance().setStyleSheet(build_style(theme))
        settings.set_many(theme=theme)

    def _build_history_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(24, 20, 24, 20)
        col.setSpacing(8)

        col.addWidget(self._build_section_label("Flash history"))

        self._history_list = QListWidget()
        self._history_list.setObjectName("historyList")
        self._history_list.itemActivated.connect(
            self._on_history_activated
        )
        col.addWidget(self._history_list, 1)

        self._history_empty = QLabel(
            "No flashes yet \u2014 write your first USB drive"
        )
        self._history_empty.setProperty("colorRole", "muted")
        self._history_empty.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self._history_empty.setVisible(False)
        col.addWidget(self._history_empty, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        export_btn = QPushButton("Export\u2026")
        export_btn.clicked.connect(self._on_history_export)
        import_btn = QPushButton("Import\u2026")
        import_btn.clicked.connect(self._on_history_import)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_history_clear)
        buttons.addWidget(export_btn)
        buttons.addWidget(import_btn)
        buttons.addWidget(clear_btn)
        buttons.addStretch()
        col.addLayout(buttons)
        return page

    def _on_history_export(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export history",
            os.path.join(settings.get("last_iso_dir") or "", "flint-history.json"),
            "JSON (*.json)",
        )
        if target and export_history(target):
            settings.set_many(last_iso_dir=os.path.dirname(target))
            self._show_tray_notify("History", "Exported.")
        elif target:
            QMessageBox.warning(self, "Export", "Could not write the file.")

    def _on_history_import(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Import history",
            settings.get("last_iso_dir") or "",
            "JSON (*.json)",
        )
        if not source:
            return
        settings.set_many(last_iso_dir=os.path.dirname(source))
        existing = len(load_history())
        if existing > 0:
            prompt = QMessageBox(self)
            prompt.setIcon(QMessageBox.Icon.Warning)
            prompt.setWindowTitle("Flint \u2014 import history?")
            prompt.setText(
                f"Importing will replace your {existing} existing "
                f"entr{'y' if existing == 1 else 'ies'}."
            )
            prompt.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            replace = prompt.addButton(
                "Replace history", QMessageBox.ButtonRole.AcceptRole
            )
            prompt.setDefaultButton(prompt.buttons()[0])
            prompt.exec()
            if prompt.clickedButton() is not replace:
                return
        ok, count = import_history(source)
        if ok:
            self._reload_history()
            self._show_from_tray()
        else:
            QMessageBox.warning(
                self, "Import", "That file is not a valid Flint history."
            )

    def _on_history_clear(self) -> None:
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Icon.Warning)
        prompt.setWindowTitle("Flint \u2014 clear history?")
        prompt.setText("Remove all flash history entries?")
        yes = prompt.addButton("Clear", QMessageBox.ButtonRole.AcceptRole)
        prompt.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        prompt.exec()
        if prompt.clickedButton() is yes:
            clear_history()
            self._reload_history()

    def _build_verify_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(24, 20, 24, 20)
        col.setSpacing(14)

        col.addWidget(self._build_section_label("Verify a drive"))

        hint = QLabel(
            "Rereads the selected drive and compares it against the "
            "image on disk."
        )
        hint.setProperty("colorRole", "muted")
        col.addWidget(hint)

        self._verify_zone = IsoDropZone()
        col.addWidget(self._verify_zone)

        self._verify_target = QLabel("Target: no drive selected")
        self._verify_target.setProperty("colorRole", "label")
        col.addWidget(self._verify_target)

        self._verify_sha_input = ShaInput()
        self._verify_sha_input.setPlaceholderText(
            "\u2026or paste an expected SHA-256 for a whole-drive check"
        )
        col.addWidget(self._verify_sha_input)

        self._verify_progress = ProgressArea()
        self._verify_progress.set_verifying()
        self._verify_progress._title.setText("Idle")
        col.addWidget(self._verify_progress)

        self._verify_mode = QLabel("")
        self._verify_mode.setObjectName("capLabel")
        self._verify_mode.setProperty("colorRole", "muted")
        col.addWidget(self._verify_mode)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._verify_cancel_btn = QPushButton("Cancel")
        self._verify_cancel_btn.setObjectName("ghost")
        self._verify_cancel_btn.setEnabled(False)
        self._verify_cancel_btn.clicked.connect(self._on_page_verify_cancel)
        self._verify_start_btn = QPushButton("Run verification")
        self._verify_start_btn.setObjectName("primary")
        self._verify_start_btn.setMinimumHeight(
            style.DESIGN_TOKENS["button_height"]
        )
        self._verify_start_btn.clicked.connect(self._on_page_verify_start)
        buttons.addWidget(self._verify_cancel_btn)
        buttons.addWidget(self._verify_start_btn, 1)
        col.addLayout(buttons)
        return page

    def _on_page_verify_start(self) -> None:
        if self._busy():
            self._verify_progress.set_error(
                "Wait for the current operation to finish first"
            )
            return
        if not self._current_drive:
            self._verify_progress.set_error(
                "Select a USB drive first \u2014 click the drive card"
            )
            return
        iso = self._verify_zone.path
        digest = self._verify_zone.digest
        pasted = self._verify_sha_input.text().strip().lower()
        if pasted:
            if len(pasted) != 64 or any(
                c not in "0123456789abcdef" for c in pasted
            ):
                self._verify_progress.set_error(
                    "Expected SHA-256 must be 64 hex characters"
                )
                return
            expected, size = pasted, None
        elif iso and digest:
            expected, size = digest, os.path.getsize(iso)
        else:
            self._verify_progress.set_error(
                "Select an image \u2014 or paste a SHA-256 \u2014 first"
            )
            return
        drive_path = self._current_drive_path()
        if not drive_path:
            self._verify_progress.set_error("Drive path unavailable")
            return
        if pasted:
            self._verify_mode.setText(
                "Comparing against: pasted SHA-256 (whole drive)"
            )
        else:
            self._verify_mode.setText(
                f"Comparing against: {os.path.basename(iso)}"
            )
        verifier = VerifyWorker(drive_path, expected, size)
        self._page_verifier = verifier
        self._verify_progress.reset()
        self._verify_progress.set_verifying()
        self._verify_start_btn.setEnabled(False)
        self._verify_cancel_btn.setEnabled(True)
        verifier.progress.connect(self._on_page_verify_progress)
        verifier.stats.connect(self._on_page_verify_stats)
        verifier.finished.connect(self._on_page_verify_finished)
        verifier.start()

    def _on_page_verify_progress(self, percent: float) -> None:
        self._verify_progress.set_progress(percent)
        self._set_taskbar_progress(percent)

    def _on_page_verify_stats(self, written: int, total: int) -> None:
        self._verify_progress.set_stats_written(written, total)

    def _on_page_verify_finished(self, ok: bool, message: str) -> None:
        worker = self._page_verifier
        self._page_verifier = None
        if worker is not None:
            self._retire(worker)
        self._verify_start_btn.setEnabled(True)
        self._verify_cancel_btn.setEnabled(False)
        self._set_taskbar_progress(None)
        self._verify_mode.setText("")
        if ok:
            self._verify_progress.set_done()
            self._verify_progress._title.setText("Verified")
        else:
            self._verify_progress.set_error(
                self._friendly_error(message or "Verification failed")
            )

    def _on_page_verify_cancel(self) -> None:
        if self._page_verifier is not None:
            self._page_verifier.cancel()

    def _on_nav_clicked(self, index: int) -> None:
        if self._busy():
            return
        for i, item in enumerate(self._nav_items):
            item.set_active(i == index)
        page = {0: 0, 1: 2, 2: 1}[index]
        if page == 1:
            self._reload_history()
        self._pages.setCurrentIndex(page)

    def _set_active_nav(self, index: int) -> None:
        for i, item in enumerate(self._nav_items):
            item.set_active(i == index)

    def _reload_history(self) -> None:
        self._history_list.clear()
        entries = load_history()
        has_entries = bool(entries)
        self._history_list.setVisible(has_entries)
        self._history_empty.setVisible(not has_entries)
        for entry in reversed(entries):
            marker = "\u2713" if entry.get("success") else "\u2715"
            item = QListWidgetItem(
                f"{marker}  {entry.get('iso', '?')}  \u2192  "
                f"{entry.get('drive', '?')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, entry)
            duration = entry.get("duration", 0) or 0
            verified = "Yes" if entry.get("verified") else "No"
            outcome = "\u2713 complete" if entry.get("success") else "\u2715 failed"
            item.setToolTip(
                f"{entry.get('timestamp', '')} \u00b7 "
                f"{round(float(duration))} s \u00b7 "
                f"Verification: {verified} \u00b7 {outcome}"
            )
            self._history_list.addItem(item)

    def _on_history_activated(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        box = QMessageBox(self)
        box.setWindowTitle("Flash report")
        box.setFont(QFont("Cascadia Mono", 9))
        box.setText(self._build_report_text(entry))
        box.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        copy_btn = box.addButton("Copy", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is copy_btn:
            QApplication.clipboard().setText(
                self._build_report_text(entry)
            )
            if self._tray is not None:
                self._tray.showMessage(
                    "Flint",
                    "Flash report copied to clipboard.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )

    def _make_flint_icon(self) -> QIcon:
        icon = QIcon()
        for size in (16, 32, 48, 64, 256):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            corner = size * 0.22
            inset = max(1, round(size * 0.06))
            painter.setBrush(QColor("#0a0a0a"))
            painter.drawRoundedRect(0, 0, size, size, corner, corner)
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(
                inset,
                inset,
                size - 2 * inset,
                size - 2 * inset,
                max(1, round(corner - size * 0.06)),
                max(1, round(corner - size * 0.06)),
            )
            painter.setBrush(QColor("#0a0a0a"))
            points = [
                QPointF(size * 0.50, size * 0.14),
                QPointF(size * 0.86, size * 0.30),
                QPointF(size * 0.86, size * 0.62),
                QPointF(size * 0.50, size * 0.86),
                QPointF(size * 0.14, size * 0.62),
                QPointF(size * 0.14, size * 0.30),
            ]
            painter.drawPolygon(points)
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(
                QPointF(size * 0.5, size * 0.5),
                size * 0.14,
                size * 0.14,
            )
            painter.end()
            icon.addPixmap(pixmap)
        return icon

    def _setup_tray(self) -> None:
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self._make_flint_icon(), self)
        self._tray.setToolTip("Flint")
        self._tray.activated.connect(self._on_tray_activated)
        menu = QMenu(self)
        show_action = menu.addAction("Show Flint")
        show_action.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._on_tray_quit)
        self._tray.setContextMenu(menu)
        self._tray.show()

    def _on_tray_quit(self) -> None:
        if self._busy():
            prompt = QMessageBox(self)
            prompt.setIcon(QMessageBox.Icon.Warning)
            prompt.setWindowTitle("Flint \u2014 still writing")
            prompt.setText(
                "A write or verification is still in progress.\n"
                "Quitting now may leave the drive unusable."
            )
            prompt.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            quit_anyway = prompt.addButton(
                "Quit anyway", QMessageBox.ButtonRole.AcceptRole
            )
            prompt.exec()
            if prompt.clickedButton() is not quit_anyway:
                return
        self._save_settings()
        self._shutdown()
        QApplication.instance().quit()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_from_tray()

    def _init_taskbar(self) -> None:
        try:
            ole32 = ctypes.windll.ole32
            guid = ctypes.c_ubyte * 16
            clsid = guid()
            iid = guid()
            ole32.CoInitialize(None)
            ole32.CLSIDFromString(
                ctypes.c_wchar_p(
                    "{56FDF344-FD6D-11D0-958A-006097C9A090}"
                ),
                ctypes.byref(clsid),
            )
            ole32.IIDFromString(
                ctypes.c_wchar_p(
                    "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
                ),
                ctypes.byref(iid),
            )
            ptr = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid),
                None,
                23,
                ctypes.byref(iid),
                ctypes.byref(ptr),
            )
            if hr != 0 or not ptr.value:
                return
            vtbl = ctypes.cast(
                ptr, ctypes.POINTER(ctypes.c_void_p)
            )
            hrinit = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p
            )(vtbl[3])
            hrinit(ptr)
            self._tb = (ptr, vtbl)
        except Exception as exc:
            logger.exception("_init_taskbar failed to initialize COM taskbar integration")
            self._tb = None

    def _set_taskbar_progress(
        self, percent: float | None, error: bool = False
    ) -> None:
        if self._tb is None:
            if self._tb_tried:
                return
            self._tb_tried = True
            self._init_taskbar()
            if self._tb is None:
                return
        ptr, vtbl = self._tb
        hwnd = ctypes.c_void_p(0)
        try:
            hwnd = ctypes.c_void_p(int(self.winId()))
        except RuntimeError:
            return
        set_value = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulonglong,
            ctypes.c_ulonglong,
        )(vtbl[9])
        set_state = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
        )(vtbl[10])
        if percent is None:
            set_state(ptr, hwnd, 4 if error else 0)  # TBPF_ERROR / NOPROGRESS
        else:
            set_state(ptr, hwnd, 2)  # TBPF_NORMAL
            set_value(ptr, hwnd, round(percent), 100)

    def _show_from_tray(self) -> None:
        self._lifecycle_log(
            f"Show tray writing={self._writing} "
            f"visible={self.isVisible()} minimized={self.isMinimized()} "
            f"native_visible={self._native_is_visible()}"
        )
        if self.isMinimized():
            self.showNormal()
        self._force_show()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._lifecycle_log(
                f"changeEvent minimized={self.isMinimized()} "
                f"visible={self.isVisible()} writing={self._writing}"
            )
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self._writing
        ):
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def event(self, e) -> bool:
        if e.type() in (
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.Close,
        ):
            self._lifecycle_log(
                f"event {e} visible={self.isVisible()} "
                f"parent={self.parentWidget()}"
            )
        return super().event(e)

    def _save_settings(self) -> None:
        settings.set_many(
            window_geometry=bytes(
                self.saveGeometry().toBase64().data()
            ).decode("ascii"),
            verify_after_write=self._verify_toggle.isChecked(),
        )

    def _shutdown(self) -> None:
        self._poller.requestInterruption()
        self._poller.wait(5000)
        for worker in self._retired_workers:
            if worker is not None and worker.isRunning():
                worker.wait(2000)
            worker.deleteLater()
        self._retired_workers.clear()

    def _tray_close_prompt(self) -> str | None:
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Icon.Information)
        prompt.setWindowTitle("Flint \u2014 keep running?")
        prompt.setText(
            "Closing the window keeps Flint running in the tray.\n"
            "A drive write or verification continues in the background."
        )
        hide_btn = prompt.addButton(
            "Keep running in tray", QMessageBox.ButtonRole.AcceptRole
        )
        quit_btn = prompt.addButton(
            "Quit Flint", QMessageBox.ButtonRole.DestructiveRole
        )
        prompt.setDefaultButton(prompt.buttons()[0])
        prompt.exec()
        clicked = prompt.clickedButton()
        if clicked is hide_btn:
            return "keep"
        if clicked is quit_btn:
            return "quit"
        return None

    def closeEvent(self, event) -> None:
        self._lifecycle_log(
            f"closeEvent writing={self._writing} "
            f"visible={self.isVisible()}"
        )
        if self._busy():
            event.ignore()
            self.hide()
            return
        self._save_settings()
        if self._tray is None:
            self._shutdown()
            QApplication.instance().quit()
            event.accept()
            return
        if not settings.get("tray_hint_seen"):
            choice = self._tray_close_prompt()
            if choice is None:
                event.ignore()
                return
            settings.set_many(tray_hint_seen=True)
            if choice == "quit":
                self._shutdown()
                QApplication.instance().quit()
                event.accept()
                return
        event.accept()

    def _lifecycle_log(self, message: str) -> None:
        try:
            log_path = os.path.join(
                os.environ.get("TEMP", "."), "flint-startup.log"
            )
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"window pid={os.getpid()}: {message}\n")
        except OSError:
            pass

    def _native_is_visible(self) -> bool:
        hwnd = int(self.winId())
        return bool(hwnd) and bool(
            ctypes.windll.user32.IsWindowVisible(hwnd)
        )

    def _force_show(self) -> None:
        self._lifecycle_log(
            f"_force_show begin: native_visible={self._native_is_visible()}"
        )
        hwnd = int(self.winId())
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            user32.ShowWindowAsync(hwnd, 5)
            self.show()
            self.raise_()
            self.activateWindow()
            user32.SetForegroundWindow(hwnd)
            self._lifecycle_log(
                f"_force_show done: native_visible="
                f"{self._native_is_visible()}"
            )
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _build_bottombar(self) -> QWidget:
        bottombar = QWidget()
        row = QHBoxLayout(bottombar)
        row.setContentsMargins(24, 14, 24, 14)
        row.setSpacing(8)

        self._wipe_btn = QPushButton("Wipe drive")
        self._wipe_btn.setObjectName("ghost")
        self._wipe_btn.clicked.connect(self._on_wipe_clicked)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("ghost")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        self._flash_btn = QPushButton("Flash drive")
        self._flash_btn.setObjectName("primary")
        self._flash_btn.setMinimumHeight(style.DESIGN_TOKENS["button_height"])
        self._flash_btn.clicked.connect(self._on_flash_clicked)

        self._verify_toggle = ToggleSwitch()
        verify_label = QLabel("Verify after write")
        verify_label.setObjectName("verifyLabel")
        verify_box = QHBoxLayout()
        verify_box.setSpacing(7)
        verify_box.addWidget(self._verify_toggle)
        verify_box.addWidget(verify_label)

        row.addWidget(self._wipe_btn)
        row.addWidget(self._cancel_btn)
        row.addWidget(self._flash_btn, 1)
        row.addStretch()
        row.addLayout(verify_box)
        return bottombar

    def _build_topbar(self) -> QWidget:
        topbar = QWidget()
        row = QHBoxLayout(topbar)
        row.setContentsMargins(24, 18, 24, 18)
        row.setSpacing(6)

        left = QVBoxLayout()
        left.setSpacing(2)
        title = QLabel("Write bootable USB")
        title.setObjectName("title")
        self._subtitle = QLabel("No drive selected")
        self._subtitle.setObjectName("subtitle")
        left.addWidget(title)
        left.addWidget(self._subtitle)

        refresh = QPushButton("\u21bb")
        refresh.setObjectName("iconBtn")
        refresh.setFixedSize(30, 30)
        refresh.setToolTip("Refresh drives (F5)")
        self._refresh_btn = refresh
        dots = QPushButton("\u22ef")
        dots.setObjectName("iconBtn")
        dots.setFixedSize(30, 30)
        dots.setToolTip("Options \u2014 themes, reset window")
        self._dots_btn = dots

        row.addLayout(left)
        row.addStretch()
        row.addWidget(refresh)
        row.addWidget(dots)
        return topbar

    def _build_content(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(24, 20, 24, 20)
        col.setSpacing(14)

        col.addWidget(self._build_section_label("Image source"))
        self._iso_zone = IsoDropZone()
        col.addWidget(self._iso_zone)

        hint = QLabel(
            "The image is written to the drive as-is (raw image mode)."
        )
        hint.setObjectName("capLabel")
        hint.setProperty("colorRole", "muted")
        col.addWidget(hint)

        self._target_card = DriveChip()
        self._target_card.setObjectName("driveCard")
        self._target_card.setCursor(Qt.CursorShape.PointingHandCursor)
        self._target_card.clicked.connect(self._show_drive_picker)
        target_row = QHBoxLayout(self._target_card)
        target_row.setContentsMargins(14, 10, 14, 10)
        target_row.setSpacing(10)
        target_col = QVBoxLayout()
        target_col.setSpacing(2)
        self._target_title = QLabel("Target drive")
        self._target_title.setObjectName("capLabel")
        self._target_detail = QLabel("No drive selected \u2014 click to choose")
        self._target_detail.setObjectName("driveName")
        self._target_detail.setProperty("dim", True)
        target_col.addWidget(self._target_title)
        target_col.addWidget(self._target_detail)
        target_row.addLayout(target_col)
        target_row.addStretch()
        self._target_change = QLabel("\u21bb Choose\u2026")
        self._target_change.setObjectName("driveSub")
        target_row.addWidget(self._target_change)

        self._target_admin_btn = QPushButton("Run as administrator")
        self._target_admin_btn.setObjectName("ghost")
        self._target_admin_btn.clicked.connect(self._relaunch_elevated)
        self._target_admin_btn.setVisible(False)
        target_row.addWidget(self._target_admin_btn)

        self._progress = ProgressArea()
        self._progress.set_ready()
        col.addWidget(self._progress)

        self._verify_hint = QLabel(
            "Verification reads the drive back after writing \u2014 "
            "it can take as long as the write itself."
        )
        self._verify_hint.setObjectName("verifyHint")
        self._verify_hint.setVisible(False)
        col.addWidget(self._verify_hint)

        steps = QLabel(
            "1. Pick an image \u00b7 2. Choose a target drive \u00b7 "
            "3. Flash, then verify"
        )
        steps.setObjectName("capLabel")
        steps.setProperty("colorRole", "muted")
        col.addWidget(steps)

        self._done_bar = QFrame()
        self._done_bar.setObjectName("block")
        done_row = QHBoxLayout(self._done_bar)
        done_row.setContentsMargins(16, 12, 16, 12)
        done_row.setSpacing(8)
        done_text = QVBoxLayout()
        done_text.setSpacing(2)
        self._done_label = QLabel("Flash complete")
        self._done_label.setObjectName("progTitle")
        self._done_summary = QLabel("")
        self._done_summary.setObjectName("doneSummary")
        done_text.addWidget(self._done_label)
        done_text.addWidget(self._done_summary)
        self._reflash_btn = QPushButton("Flash again")
        self._eject_btn = QPushButton("Eject drive")
        self._copy_btn = QPushButton("Copy report")
        self._reflash_btn.clicked.connect(self._on_flash_clicked)
        self._eject_btn.clicked.connect(self._on_eject_clicked)
        self._copy_btn.clicked.connect(self._on_copy_report_clicked)
        done_row.addLayout(done_text)
        done_row.addStretch()
        done_row.addWidget(self._reflash_btn)
        done_row.addWidget(self._eject_btn)
        done_row.addWidget(self._copy_btn)
        self._done_bar.setVisible(False)
        col.addWidget(self._done_bar)

        scroll.setWidget(content)
        return scroll

    def _build_section_label(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("capLabel")
        return label

    def _build_iso_zone(self) -> QFrame:
        return IsoDropZone()

    def _current_drive_path(self) -> str | None:
        return self._drive_path_for(self._current_drive)

    def _drive_path_for(self, drive: dict | None) -> str | None:
        if not drive:
            return None
        path = drive["physical_path"]
        if path.startswith("\\\\.\\"):
            return path
        return f"\\\\.\\{path}"

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._controls:
            widget.setEnabled(enabled)
        self._flash_btn.setEnabled(enabled)

    def _update_controls_state(self) -> None:
        """Enable/disable primary actions based on selection and state."""
        busy = self._busy()
        has_iso = bool(getattr(self._iso_zone, "path", None))
        has_drive = self._current_drive is not None
        # Flash enabled when not busy and iso + drive selected
        try:
            self._flash_btn.setEnabled((not busy) and has_iso and has_drive)
        except Exception:
            pass
        # Wipe enabled when a drive is selected and not busy
        try:
            self._wipe_btn.setEnabled((not busy) and has_drive)
        except Exception:
            pass
        # Verify page start button: disable if no drive or busy
        try:
            if hasattr(self, "_verify_start_btn"):
                self._verify_start_btn.setEnabled((not busy) and has_drive)
        except Exception:
            pass

    def _on_cancel_clicked(self) -> None:
        if self._writer is not None:
            self._writer.cancel()
        if self._verifier is not None:
            self._verifier.cancel()
        if self._page_verifier is not None:
            self._page_verifier.cancel()
        if self._wipe_worker is not None:
            self._wipe_worker.cancel()

    def _recheck_drive(self, drive: dict) -> dict | None:
        drives = self._detector.list_removable_drives()
        current = next(
            (
                d
                for d in drives
                if d.get("physical_path") == drive.get("physical_path")
            ),
            None,
        )
        if current is None:
            return None
        if (
            drive.get("serial")
            and current.get("serial")
            and drive["serial"] != current["serial"]
        ):
            return None
        return current

    def _on_flash_clicked(self) -> None:
        if self._busy():
            return
        iso = self._iso_zone.path
        if not iso:
            self._progress.set_error("Select an ISO image first")
            return
        if not self._current_drive:
            self._progress.set_error("No USB drive connected")
            return
        drive = self._current_drive
        name = drive["model"] or drive["name"]
        iso_name = os.path.basename(iso)
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Icon.Warning)
        prompt.setWindowTitle("Flint \u2014 erase drive and write?")
        prompt.setText(
            f"Erase {name} ({drive['letter']}:) and write {iso_name}?"
        )
        prompt.setInformativeText(self._confirm_text(drive, iso))
        prompt.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        erase = prompt.addButton(
            "Erase & write", QMessageBox.ButtonRole.AcceptRole
        )
        prompt.setDefaultButton(
            prompt.buttons()[0]
        )
        prompt.exec()
        if prompt.clickedButton() is not erase:
            return

        current = self._recheck_drive(drive)
        if current is None:
            self._progress.set_error(
                "Drive changed or disconnected \u2014 refresh and re-pick"
            )
            return
        drive_path = self._drive_path_for(current)
        if not drive_path:
            self._progress.set_error("Drive path unavailable")
            return
        # Require typed confirmation for destructive actions
        if not self._require_typed_confirmation(current, iso):
            self._progress.set_error("Confirmation failed — aborting")
            return
        self._current_drive = current
        self._active_write_drive = current

        self._progress.reset()
        self._done_bar.setVisible(False)
        self._set_controls_enabled(False)
        self._cancel_btn.setEnabled(True)
        self._wipe_btn.setEnabled(False)
        self._writing = True
        self._write_started = time.perf_counter()
        if self._tray is not None:
            self._tray.setToolTip("Flint \u2014 Writing\u2026")

        drive_letters = current.get("letters") or (
            [current["letter"]] if current.get("letter") else []
        )
        writer = UsbWriter(
            iso,
            drive_path,
            letters=drive_letters,
        )
        self._writer = writer
        writer.progress.connect(self._on_write_progress)
        writer.speed_mbps.connect(self._progress.set_speed)
        writer.written_bytes.connect(self._progress.set_written)
        writer.total_bytes.connect(self._progress.set_total)
        writer.eta_seconds.connect(self._progress.set_eta)
        writer.phase.connect(self._progress.set_phase)
        writer.finished.connect(self._on_write_finished)
        writer.start()

    def _on_write_progress(self, percent: float) -> None:
        self._progress.set_progress(percent)
        self._set_taskbar_progress(percent)
        if self._tray is not None:
            self._tray.setToolTip(f"Flint \u2014 {percent:.0f}%")

    def _on_write_finished(self, ok: bool, message: str) -> None:
        self._write_duration = time.perf_counter() - self._write_started
        worker = self._writer
        self._writer = None
        if worker is not None:
            self._retire(worker)
        if not ok:
            self._finish_flash(False, message or "Write failed", None)
            return
        if self._verify_toggle.isChecked():
            if self._iso_zone.digest:
                self._start_verify()
                return
            self._finish_flash(True, "", None, skipped_verify=True)
            return
        self._finish_flash(True, "", None)

    def _retire(self, worker: QThread) -> None:
        self._retired_workers.append(worker)

    def _start_verify(self) -> None:
        iso = self._iso_zone.path
        target = self._active_write_drive or self._current_drive
        drive_path = self._drive_path_for(target)
        digest = self._iso_zone.digest
        if not iso or not drive_path or digest is None:
            self._finish_flash(
                False, "Verification couldn't start: ISO digest missing", None
            )
            return
        self._writing = True
        self._progress.set_verifying()
        self._verify_hint.setVisible(True)
        if self._tray is not None:
            self._tray.setToolTip("Flint \u2014 Verifying\u2026")
        verifier = VerifyWorker(
            drive_path, digest, os.path.getsize(iso)
        )
        self._verifier = verifier
        verifier.progress.connect(self._on_verify_progress)
        verifier.stats.connect(self._on_verify_stats)
        verifier.finished.connect(self._on_verify_finished)
        verifier.start()

    def _on_verify_stats(self, written: int, total: int) -> None:
        self._progress.set_stats_written(written, total)

    def _on_verify_progress(self, percent: float) -> None:
        self._progress.set_progress(percent)
        self._set_taskbar_progress(percent)
        if self._tray is not None:
            self._tray.setToolTip(
                f"Flint \u2014 Verifying\u2026 {percent:.0f}%"
            )

    def _on_verify_finished(self, ok: bool, message: str) -> None:
        worker = self._verifier
        self._verifier = None
        if worker is not None:
            self._retire(worker)
        if ok:
            self._finish_flash(True, "", message)
        else:
            self._finish_flash(False, message or "Verification failed", None)

    def _on_wipe_clicked(self) -> None:
        if self._busy():
            return
        if not self._current_drive:
            self._progress.set_error("Select a USB drive first")
            return
        drive = self._current_drive
        name = drive.get("model") or drive.get("name")
        letter = drive.get("letter") or "no drive letter"
        prompt = QMessageBox(self)
        prompt.setIcon(QMessageBox.Icon.Warning)
        prompt.setWindowTitle("Flint \u2014 erase drive?")
        prompt.setText(
            f"Erase {name} ({letter}) \u2014 replace every byte with zeros?"
        )
        prompt.setInformativeText(self._confirm_text(drive))
        prompt.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        erase = prompt.addButton(
            "Erase & wipe", QMessageBox.ButtonRole.AcceptRole
        )
        prompt.setDefaultButton(prompt.buttons()[0])
        prompt.exec()
        if prompt.clickedButton() is not erase:
            return

        current = self._recheck_drive(drive)
        if current is None:
            self._progress.set_error(
                "Drive changed or disconnected \u2014 refresh and re-pick"
            )
            return
        drive_path = self._drive_path_for(current)
        if not drive_path:
            self._progress.set_error("Drive path unavailable")
            return
        # Require typed confirmation for destructive actions
        if not self._require_typed_confirmation(current, None):
            self._progress.set_error("Confirmation failed — aborting")
            return
        self._current_drive = current
        self._active_write_drive = current
        letters = current.get("letters") or (
            [current["letter"]] if current.get("letter") else []
        )
        self._progress.reset()
        self._done_bar.setVisible(False)
        self._set_controls_enabled(False)
        self._cancel_btn.setEnabled(True)
        self._flash_btn.setEnabled(False)
        self._writing = True
        self._write_started = time.perf_counter()
        if self._tray is not None:
            self._tray.setToolTip("Flint \u2014 Wiping\u2026")

        worker = WipeWorker(drive_path, letters)
        self._wipe_worker = worker
        worker.progress.connect(self._on_write_progress)
        worker.speed_mbps.connect(self._progress.set_speed)
        worker.written_bytes.connect(self._progress.set_written)
        worker.total_bytes.connect(self._progress.set_total)
        worker.eta_seconds.connect(self._progress.set_eta)
        worker.phase.connect(self._progress.set_phase)
        worker.finished.connect(self._on_wipe_finished)
        worker.start()

    def _on_wipe_finished(self, ok: bool, message: str) -> None:
        worker = self._wipe_worker
        self._wipe_worker = None
        if worker is not None:
            self._retire(worker)
        self._writing = False
        self._set_controls_enabled(True)
        self._cancel_btn.setEnabled(False)
        self._set_taskbar_progress(None, error=not ok)
        target = self._active_write_drive or self._current_drive
        if ok:
            self._progress.set_done()
            self._progress._title.setText("Wiped")
            self._done_label.setText("Drive wiped")
            self._done_bar.setVisible(True)
            if self._tray is not None:
                self._tray.showMessage(
                    "Flint \u2014 Wipe finished",
                    f"{target.get('model') or 'drive'} wiped."
                    if target is not None
                    else "Drive wiped.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
        else:
            if message == "cancelled":
                self._progress.set_error(
                    "Wipe cancelled \u2014 the drive was partially wiped. "
                    "Run wipe again to finish erasing it."
                )
            else:
                self._progress.set_error(
                    self._friendly_error(message or "Wipe failed")
                )
        if target is not None:
            append_history(
                flash_report(
                    "\u2014 wipe \u2014",
                    target["model"] or target["name"],
                    time.perf_counter() - self._write_started,
                    verified=False,
                    success=ok,
                    drive_serial=target.get("serial"),
                )
            )
        if self._tray is not None:
            self._tray.setToolTip(
                "Flint \u2014 Wipe done" if ok else "Flint"
            )
        self._active_write_drive = None

    def _on_eject_clicked(self) -> None:
        path = self._current_drive_path()
        if not path:
            QMessageBox.information(
                self, "Eject", "No drive selected."
            )
            return
        try:
            ok, msg = eject_drive(path)
        except Exception as exc:
            ok, msg = False, str(exc) or "eject failed"
        if ok:
            QMessageBox.information(
                self, "Eject", "Drive ejected \u2014 safe to unplug."
            )
        else:
            QMessageBox.warning(
                self, "Eject", msg or "Windows refused to eject the drive."
            )

    @staticmethod
    def _friendly_error(message: str) -> str:
        lowered = message.lower()
        if "drive not writable" in lowered or "could not open drive" in lowered:
            return (
                f"{message} \u2014 re-check the drive and run Flint "
                "as administrator."
            )
        if "write failed" in lowered or "flush failed" in lowered:
            return (
                f"{message} \u2014 the drive may be locked, removed or "
                "failing. Re-insert it and try again."
            )
        if "short write" in lowered or "short read" in lowered:
            return (
                f"{message} \u2014 the drive accepted fewer bytes than "
                "expected. The result may be incomplete."
            )
        return message

    def _on_copy_report_clicked(self) -> None:
        if self._last_report is None:
            return
        text = self._build_report_text(self._last_report)
        QApplication.clipboard().setText(text)
        if self._tray is not None:
            self._tray.showMessage(
                "Flint",
                "Flash report copied to clipboard.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    @staticmethod
    def _build_report_text(entry: dict) -> str:
        lines = [
            "Flint flash report",
            f"Date: {entry.get('timestamp', '?')}",
            f"Image: {entry.get('iso', '?')}",
            f"Drive: {entry.get('drive', '?')}",
            f"Duration: {entry.get('duration', 0)} s",
            f"Average speed: {entry.get('avg_mbps')} MB/s"
            if entry.get("avg_mbps") is not None
            else "Average speed: n/a",
            f"Verified: {'yes' if entry.get('verified') else 'no'}",
            f"Boot: {entry.get('bootable') or 'unknown'}",
            f"ISO SHA-256: {entry.get('iso_sha256') or '-'}",
            f"Drive read-back: {entry.get('written_sha256') or '-'}"
            if entry.get("written_sha256")
            else "Drive read-back: -",
        ]
        return "\n".join(lines)

    def _show_tray_notify(self, title: str, message: str) -> None:
        if self._tray is not None:
            self._tray.showMessage(
                title, message, QSystemTrayIcon.MessageIcon.Information, 4000
            )

    def _finish_flash(
        self,
        succeeded: bool,
        error_text: str,
        verified_sha: str | None,
        skipped_verify: bool = False,
    ) -> None:
        self._writing = False
        self._verify_hint.setVisible(False)
        self._set_controls_enabled(True)
        self._cancel_btn.setEnabled(False)
        self._wipe_btn.setEnabled(self._current_drive is not None)
        self._set_taskbar_progress(None, error=not succeeded)
        self._writer = None
        self._verifier = None

        target = self._active_write_drive or self._current_drive

        boot: str | None = None
        if (
            succeeded
            and target is not None
            and self._iso_zone.path is not None
        ):
            try:
                probe = probe_bootability(self._drive_path_for(target) or "")
                if probe.get("gpt") and probe.get("mbr_signature"):
                    boot = "GPT + MBR"
                elif probe.get("gpt"):
                    boot = "GPT (UEFI)"
                elif probe.get("mbr_signature"):
                    boot = "MBR (legacy)"
                elif probe.get("efi_partition"):
                    boot = "MBR (bootable)"
                else:
                    boot = "no boot signature"
            except Exception as exc:
                logger.exception("probe_bootability failed")
                boot = "unknown"

        if succeeded:
            self._progress.set_done()
            if skipped_verify:
                self._progress.set_warning(
                    "Verification skipped \u2014 the image digest is "
                    "unavailable. Re-select the image and flash again to "
                    "verify."
                )
                self._done_label.setText("Flash complete \u2014 not verified")
            else:
                self._done_label.setText(
                    "Verified \u2713" if verified_sha else "Flash complete"
                )
            self._done_bar.setVisible(True)
        elif error_text == "cancelled":
            self._progress.set_error(
                "Write cancelled \u2014 the drive was left partially "
                "written and is not usable. Flash again before using it."
            )
            self._done_label.setText("Write cancelled")
            self._done_summary.setText(
                "The drive needs a complete write before use."
            )
            self._done_bar.setVisible(True)
        else:
            self._progress.set_error(
                self._friendly_error(error_text or "Failed")
            )
            self._done_bar.setVisible(False)

        if boot and succeeded:
            self._done_summary.setText(f"Boot: {boot}")
            self._done_summary.setToolTip(
                "GPT (UEFI): modern firmware \u00b7 "
                "MBR (legacy): older firmware"
            )
        elif succeeded:
            self._done_summary.setText("")
            self._done_summary.setToolTip("")

        report: dict | None = None
        if target is not None and self._iso_zone.path is not None:
            iso_size = os.path.getsize(self._iso_zone.path)
            avg_mbps = (
                iso_size / 1_000_000 / self._write_duration
                if self._write_duration > 0
                else None
            )
            report = flash_report(
                os.path.basename(self._iso_zone.path),
                target["model"] or target["name"],
                self._write_duration,
                verified=verified_sha is not None and succeeded,
                success=succeeded,
                iso_sha256=self._iso_zone.digest,
                written_sha256=verified_sha if succeeded else None,
                drive_serial=target.get("serial"),
                bootable=boot,
                avg_mbps=avg_mbps,
            )
            append_history(report)
        self._last_report = report
        self._active_write_drive = None
        if self._tray is not None:
            self._tray.setToolTip(
                "Flint \u2014 Done" if succeeded else "Flint"
            )
            if succeeded:
                self._tray.showMessage(
                    "Flint \u2014 Flash finished",
                    "The image was "
                    + ("verified." if verified_sha else "written.")
                    + " Click the tray icon to open Flint.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )