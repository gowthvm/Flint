import ctypes
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (  # type: ignore[attr-defined]
    QByteArray,
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QEnterEvent,
    QGuiApplication,
    QHideEvent,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QMoveEvent,
    QPainter,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from core import checksum as checksum_mod
from core import fleet, settings
from core import iso as iso_mod
from core.backup import BackupWorker
from core.bootcheck import probe_bootability
from core.clone import CloneWorker
from core.drives import DriveDetector, DrivePoller
from core.eject import eject_drive
from core.fleet import FleetSession
from core.history import (
    append_history,
    clear_history,
    export_history,
    flash_report,
    import_history,
    load_history,
)
from core.updates import (
    UpdateCheckWorker,
    UpdateDownloadWorker,
    compare_version,
    default_download_path,
    fetch_sidecar_digest,
    release_executable,
    sidecar_digest_url,
    version_from_tag,
)
from core.verify import VerifyWorker
from core.version import APP_VERSION
from core.wipe import WipeWorker
from core.writer import UsbWriter
from ui import dialogs, style
from ui.chamfer import ChamferPanel

logger = logging.getLogger("flint")

_HELP_TIPS = {
    "partition_scheme": (
        "How the drive is partitioned in file-copy mode: GPT for modern "
        "UEFI, MBR for legacy BIOS, Auto picks GPT for UEFI targets. "
        "Raw (DD) writes ignore this."
    ),
    "target_system": (
        "UEFI (EFI boot path) or Legacy (BIOS). Influences the partition "
        "scheme and where boot files are placed in file-copy mode; "
        "Windows To Go installs boot files for both."
    ),
    "filesystem": (
        "FAT32 is the most bootable but cannot store files over 4 GB. "
        "NTFS supports large files and is required for Windows To Go. "
        "Only matters in file-copy mode."
    ),
    "write_mode": (
        "Raw (DD): byte-for-byte copy, fastest and safest for bootable "
        "images. File copy: repartitions and formats the drive, then "
        "copies the ISO contents onto it. Hybrid ISOs are always raw."
    ),
    "chunk_size_mb": (
        "Write buffer in MiB (4-64). The native writer uses low-level "
        "CreateFile/WriteFile with unbuffered, sector-aligned I/O for "
        "the highest throughput; writes fall back to the Python writer "
        "when it is not built."
    ),
    "persistence": (
        "Keeps changes across reboots on Linux live sticks (Ubuntu "
        "casper-rw / Debian live persistence.conf); needs an ext4 "
        "formatting tool such as wsl mke2fs."
    ),
    "windows_to_go": (
        "Applies a Windows ISO with dism and installs boot files so the "
        "stick boots as portable Windows. Requires NTFS + file-copy mode "
        "and elevation; mutually exclusive with persistence."
    ),
    "verify_sha256": (
        "Reads the drive back after writing and compares its SHA-256 "
        "against the image, reporting mismatched offsets. Can take as "
        "long as the write; skipped after file-copy writes."
    ),
    "bad_block_scan": (
        "Re-reads the drive for unreadable sectors, retrying failed "
        "reads (1-10 times); unreadable sectors are reported at "
        "4 KiB-aligned offsets."
    ),
}


def _restyle(widget: QWidget) -> None:
    """Repolish a widget so property-driven stylesheet selectors apply."""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)


class TipBubble(QFrame):
    """Small always-on-top bubble for the (?) help buttons.

    Text wraps into a boxed card; the bubble fades in and out in a few
    tens of milliseconds. It never takes focus or mouse events, so
    hovering through it cannot swallow input.
    """

    FADE_MS = 70

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip)
        self.setObjectName("helpTip")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        self._label = QLabel("")
        self._label.setObjectName("helpTipLabel")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(300)
        lay.addWidget(self._label)
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(self.FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_fade_finished)

    def _on_fade_finished(self) -> None:
        if float(self._anim.endValue()) == 0.0:
            self.hide()

    def show_at(self, anchor: QWidget, text: str) -> None:
        self._label.setText(text)
        self.adjustSize()
        self.move(self._position_for(anchor))
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        self._fade_to(1.0)

    def hide_fast(self) -> None:
        self._fade_to(0.0)

    def _fade_to(self, goal: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(goal)
        self._anim.start()

    def _position_for(self, anchor: QWidget) -> QPoint:
        top_left = anchor.mapToGlobal(QPoint(0, 0))
        size = self.sizeHint()
        screen = QApplication.screenAt(top_left)
        if screen is None:
            screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        x = top_left.x() + (anchor.width() - size.width()) // 2
        above = top_left.y() - size.height() - 6
        if available is not None and above < available.top():
            y = top_left.y() + anchor.height() + 6
        else:
            y = above
        if available is not None:
            x = min(max(x, available.left()), available.right() - size.width())
            y = max(y, available.top())
        return QPoint(x, y)


class HelpButton(QPushButton):
    """(?) button whose tip fades in instantly as a bubble on hover."""

    def __init__(
        self, tip: str, shared_bubble: TipBubble | None = None
    ) -> None:
        super().__init__("?")
        self._tip = tip
        self._bubble = shared_bubble
        self.setObjectName("helpBtn")
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Help")
        self.setAccessibleDescription(tip)

    def tip_text(self) -> str:
        return self._tip

    def enterEvent(self, event: QEnterEvent | None) -> None:
        assert event is not None
        if self._bubble is None:
            self._bubble = TipBubble()
        self._bubble.show_at(self, self._tip)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent | None) -> None:
        assert event is not None
        if self._bubble is not None:
            self._bubble.hide_fast()
        super().leaveEvent(event)

    def hideEvent(self, event: QHideEvent | None) -> None:
        assert event is not None
        if self._bubble is not None:
            self._bubble.hide_fast()
        super().hideEvent(event)


class IsoWorker(QThread):
    hash_done = pyqtSignal(str, bool, str)
    progress = pyqtSignal(int)
    eta = pyqtSignal(int)

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        self._start_time = time.monotonic()

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
                        elapsed = time.monotonic() - self._start_time
                        if elapsed > 0 and read_bytes > 0:
                            speed = read_bytes / elapsed
                            remaining = (total - read_bytes) / speed
                            self.eta.emit(int(remaining))
            if not self.isInterruptionRequested():
                self.hash_done.emit(self._path, True, digest.hexdigest())
        except Exception:
            logger.exception("IsoWorker.run failed")
            if not self.isInterruptionRequested():
                self.hash_done.emit(self._path, False, "")


class IsoDetectWorker(QThread):
    detected = pyqtSignal(str, bool, bool, bool)  # path, is_linux, is_windows, is_hybrid

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def run(self) -> None:
        from core.iso import (
            detect_linux_iso,
            detect_windows_iso,
            is_hybrid_iso,
        )

        try:
            linux = detect_linux_iso(self._path)
            windows = detect_windows_iso(self._path)
            hybrid = is_hybrid_iso(self._path)
            self.detected.emit(self._path, linux, windows, hybrid)
        except Exception:
            logger.exception("IsoDetectWorker.run failed")
            self.detected.emit(self._path, False, False, False)


class IsoDropZone(QFrame):
    iso_selected = pyqtSignal(str)
    iso_analysis = pyqtSignal(str, bool, bool, bool)  # path, linux, windows, hybrid
    hash_done = pyqtSignal(str, bool, str)  # path, ok, digest

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("isoDropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._path: str | None = None
        self._worker: IsoWorker | None = None
        self._analyzer: IsoDetectWorker | None = None
        self._digest: str | None = None
        self._hash_finished = False
        self._retired_workers: list[QThread] = []
        self._clear_guard: Callable[[], bool] | None = None
        self._browse_guard: Callable[[], bool] | None = None
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
        text.setWordWrap(True)
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
        self._iso_name.setWordWrap(True)
        self._iso_meta = QLabel("")
        self._iso_meta.setObjectName("isoMeta")
        self._iso_meta.setWordWrap(True)
        info.addWidget(self._iso_name)
        info.addWidget(self._iso_meta)

        self._iso_check = QLabel("\u2713\ufe0e")
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

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if event.button() == Qt.MouseButton.LeftButton:
            self._browse()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        assert event is not None
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self._browse()
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        assert event is not None
        mime = event.mimeData()
        assert mime is not None
        if mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent | None) -> None:
        assert event is not None
        mime = event.mimeData()
        assert mime is not None
        urls = mime.urls()
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

    def _first_iso_url(self, event: QDropEvent) -> QUrl | None:
        mime = event.mimeData()
        if mime is None:
            return None
        for url in mime.urls():
            if (
                url.isLocalFile()
                and url.toLocalFile().lower().endswith(
                    (".iso", ".img", ".bin")
                )
            ):
                return url
        return None

    def _browse(self) -> None:
        if self._browse_guard is not None and self._browse_guard():
            return
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
        analyzer = self._analyzer
        if analyzer is not None and analyzer.isRunning():
            analyzer.requestInterruption()
            analyzer.wait(3000)
        if analyzer is not None:
            self._retired_workers.append(analyzer)
        self._analyzer = None
        self._digest = None
        self._hash_finished = False
        self._path = None
        self.iso_analysis.emit("", False, False, False)
        self._drop_error.setVisible(False)
        self._drop_timer.stop()
        self._loaded.setVisible(False)
        self._empty.setVisible(True)
        self.setProperty("loaded", False)
        _restyle(self)

    def load_iso(self, path: str) -> None:
        if not path or not os.path.isfile(path):
            return
        if self._browse_guard is not None and self._browse_guard():
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
        _restyle(self)
        self.iso_selected.emit(path)

        worker = IsoWorker(path)
        self._worker = worker
        worker.hash_done.connect(self._on_hash_done)
        worker.progress.connect(self._on_hash_progress)
        worker.eta.connect(self._on_hash_eta)
        worker.start()

        analyzer = IsoDetectWorker(path)
        self._analyzer = analyzer
        analyzer.detected.connect(self._on_analysis)
        analyzer.start()

    def _on_analysis(
        self, path: str, is_linux: bool, is_windows: bool, is_hybrid: bool
    ) -> None:
        if path != self._path:
            return
        self.iso_analysis.emit(path, is_linux, is_windows, is_hybrid)

    def _on_hash_progress(self, percent: int) -> None:
        if self._path is None or self._hash_finished:
            return
        size = DriveDetector.format_size(
            os.path.getsize(self._path) if os.path.isfile(self._path) else 0
        )
        self._set_meta(False, f"{size} \u00b7 Reading image\u2026 {percent}%")

    def _on_hash_eta(self, seconds: int) -> None:
        if self._path is None or self._hash_finished:
            return
        if seconds <= 0:
            return
        size = DriveDetector.format_size(
            os.path.getsize(self._path) if os.path.isfile(self._path) else 0
        )
        if seconds < 60:
            eta_str = f"~{seconds}s"
        elif seconds < 3600:
            eta_str = f"~{seconds // 60}m {seconds % 60}s"
        else:
            eta_str = f"~{seconds // 3600}h {(seconds % 3600) // 60}m"
        self._set_meta(False, f"{size} \u00b7 Reading image\u2026 {eta_str} remaining")

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
        self.hash_done.emit(path, ok, digest)

    def _set_meta(self, ok: bool, text: str) -> None:
        self._iso_meta.setText(text)
        self._iso_meta.setProperty("error", not ok)
        _restyle(self._iso_meta)


class ShaInput(QLineEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("shaInput")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        assert event is not None
        mime = event.mimeData()
        if mime is None or not (
            mime.hasText() or any(
                url.isLocalFile() for url in mime.urls()
            )
        ):
            event.ignore()
            return
        event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:
        assert event is not None
        mime = event.mimeData()
        if mime is None:
            return
        urls = [u for u in mime.urls() if u.isLocalFile()]
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
            _restyle(button)
            button.update()

    def _select(self, index: int) -> None:
        if index == self._active:
            return
        self._active = index
        self._value = self._options[index]
        self._apply()
        self.valueChanged.emit(self._value)

    @pyqtProperty(str, notify=valueChanged)  # type: ignore[untyped-decorator]
    def value(self) -> str:
        return self._value


class ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True) -> None:
        super().__init__()
        self.setObjectName("toggleSwitch")
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

        self._knob = QLabel()
        self._knob.setObjectName("toggleKnob")
        self._knob.setFixedSize(
            style.DESIGN_TOKENS["toggle_knob"],
            style.DESIGN_TOKENS["toggle_knob"],
        )
        self._knob.setParent(self._track)

        self._anim = QPropertyAnimation(self._knob, b"pos", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._knob.move(self._knob_x(self._checked), self._knob_y())

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._track)
        self._apply()

    @staticmethod
    def _knob_x(checked: bool) -> int:
        margin = 2
        width = style.DESIGN_TOKENS["toggle_w"]
        knob = style.DESIGN_TOKENS["toggle_knob"]
        return width - knob - margin if checked else margin

    @staticmethod
    def _knob_y() -> int:
        margin = 2
        height = style.DESIGN_TOKENS["toggle_h"]
        knob = style.DESIGN_TOKENS["toggle_knob"]
        return height - knob - margin

    def _apply(self) -> None:
        self._track.setProperty("on", self._checked)
        _restyle(self._track)
        _restyle(self._knob)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool, animate: bool = True) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._apply()
        self._anim.stop()
        target = self._knob_x(checked)
        if animate and self.isVisible():
            self._anim.setStartValue(self._knob.pos())
            self._anim.setEndValue(QPoint(target, self._knob.y()))
            self._anim.start()
        else:
            self._knob.move(target, self._knob.y())
        self.toggled.emit(checked)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        assert event is not None
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.setChecked(not self._checked)
        else:
            super().keyPressEvent(event)


class ProgressArea(ChamferPanel):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("progressArea")
        self._total = 0
        self._written = 0
        self._target_pct: float = 0.0
        self._eta_window: deque[int] = deque(maxlen=5)

        self._smooth_timer = QTimer(self)
        self._smooth_timer.setInterval(50)
        self._smooth_timer.timeout.connect(self._smooth_tick)

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
        self._target_pct = 0.0
        self._smooth_timer.stop()
        self._eta_window.clear()
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
        self._target_pct = 0.0
        self._smooth_timer.stop()
        self._eta_window.clear()
        self._title.setText("Writing\u2026")
        self._pct.setText("0%")
        self._bar.setValue(0)
        self.set_values(0, 0.0, 0)
        self._error.setVisible(False)

    def _smooth_tick(self) -> None:
        current = self._bar.value()
        diff = self._target_pct - current
        if abs(diff) < 0.5:
            self._bar.setValue(round(self._target_pct))
            if self._target_pct >= 100:
                self._smooth_timer.stop()
            return
        self._bar.setValue(round(current + diff * 0.18))

    def set_progress(self, percent: float) -> None:
        self._target_pct = percent
        self._pct.setText(f"{percent:.0f}%")
        if not self._smooth_timer.isActive():
            self._smooth_timer.start()

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
        self._eta_window.append(seconds)
        if self._eta_window:
            median = sorted(self._eta_window)[len(self._eta_window) // 2]
        else:
            median = seconds
        self._stat_values["Remaining"].setText(self._fmt_eta(median))

    def set_values(self, written: int, mbps: float, seconds: int) -> None:
        self.set_written(written)
        self.set_speed(mbps)
        self.set_eta(seconds)

    def set_verifying(self) -> None:
        self._target_pct = 0.0
        self._smooth_timer.stop()
        self._eta_window.clear()
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
        self._smooth_timer.stop()
        self._target_pct = 100.0
        self._title.setText("Done")
        self._pct.setText("100%")
        self._bar.setValue(100)

    def set_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setProperty("level", "error")
        _restyle(self._error)
        self._error.setVisible(True)

    def set_warning(self, message: str) -> None:
        self._error.setText(message)
        self._error.setProperty("level", "warning")
        _restyle(self._error)
        self._error.setVisible(True)

    @staticmethod
    def _fmt_eta(seconds: int) -> str:
        if seconds <= 0:
            return "\u2014"
        if seconds < 60:
            return f"~{seconds} s"
        minutes, _ = divmod(seconds, 60)
        if minutes < 60:
            return f"~{minutes} min"
        hours, minutes = divmod(minutes, 60)
        if minutes == 0:
            return f"~{hours} h"
        return f"~{hours} h {minutes} min"

    @staticmethod
    def _fmt_gb(num_bytes: int) -> str:
        return f"{num_bytes / 1_000_000_000:.1f} GB"


class DriveChip(ChamferPanel):
    clicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("driveChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        assert event is not None
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
        text: str,
        active: bool,
        badge: str | None = None,
    ) -> None:
        super().__init__()
        self._text = text
        self._badge = badge
        self.setObjectName("navItem")
        self.setProperty("on", active)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(9)

        label = QLabel(text)
        label.setObjectName("navText")
        label.setProperty("on", active)

        row.addWidget(label)
        row.addStretch()
        if badge is not None:
            self._badge_label: QLabel | None = QLabel(badge)
            self._badge_label.setObjectName("badgeOn" if active else "badge")
            self._badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(self._badge_label)
        else:
            self._badge_label = None

    def set_active(self, active: bool) -> None:
        self.setProperty("on", active)
        _restyle(self)
        self.update()
        if self._badge_label is not None:
            self._badge_label.setObjectName(
                "badgeOn" if active else "badge"
            )
            _restyle(self._badge_label)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        assert event is not None
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Flint")

        # Dark title bar on Windows 10/11
        try:
            import ctypes as _ctypes

            hwnd = int(self.winId())
            _ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                20,
                _ctypes.byref(_ctypes.c_int(1)),
                _ctypes.sizeof(_ctypes.c_int),
            )
        except Exception:
            pass

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.resize(
                min(900, max(640, geo.width() - 80)),
                min(580, max(440, geo.height() - 100)),
            )
            height_min = min(540, max(420, geo.height() - 100))
        else:
            self.resize(900, 580)
            height_min = 540

        self._detector = DriveDetector()
        self._current_drive: dict[str, Any] | None = None
        self._active_write_drive: dict[str, Any] | None = None
        self._drives: list[dict[str, Any]] = []
        self._writer: UsbWriter | None = None
        self._verifier: VerifyWorker | None = None
        self._page_verifier: VerifyWorker | None = None
        self._wipe_worker: WipeWorker | None = None
        self._wipe_verify: tuple[bool, str] | None = None
        self._backup_worker: BackupWorker | None = None
        self._clone_worker: CloneWorker | None = None
        self._backup_digest = ""
        self._backup_out = ""
        self._queue_items: list[str] = []
        self._queue_index = 0
        self._queue_active = False
        self._queue_ok = 0
        self._queue_last_succeeded = False
        self._fleet: FleetSession | None = None
        self._fleet_busy = False
        self._fleet_image_index = 0
        self._fleet_drive: dict[str, Any] | None = None
        self._update_checker: UpdateCheckWorker | None = None
        self._update_downloader: UpdateDownloadWorker | None = None
        self._pending_update_path = ""
        self._sidecar_status = "missing"
        self._sidecar_detail = ""
        self._retired_workers: list[QThread] = []
        # Threads that refused to stop within the shutdown grace period.
        # Destroying a running QThread aborts the process mid-write, so they
        # are kept referenced until process exit instead of being deleted.
        self._zombies: list[QThread] = []
        self._shutdown_done = False
        self._last_report: dict[str, Any] | None = None
        self._controls: list[QWidget] = []
        self._writing = False
        self._write_started = 0.0
        self._write_duration = 0.0
        self._write_was_filecopy = False
        self._verify_handled = False
        self._verification_in_writer = False
        self._last_verify_message = ""
        self._last_verify_digest = ""
        self._retry_payload: tuple[Any, ...] | None = None
        self._iso_linux = False
        self._iso_windows = False
        self._iso_hybrid = False
        self._tray: QSystemTrayIcon | None = None
        self._tb = None
        self._tb_last_try = 0.0
        self._ejecting = False

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
        self.setAcceptDrops(True)
        self._soften_minimum_widths()
        if screen is not None:
            geo = screen.availableGeometry()
            width_min = min(
                self._content_minimum_width(), max(640, geo.width() - 80)
            )
        else:
            width_min = self._content_minimum_width()
        self.setMinimumSize(width_min, height_min)

        self._controls = [
            self._iso_zone,
            self._verify_toggle,
            self._cancel_btn,
            self._refresh_btn,
            self._dots_btn,
            self._expert_toggle,
            self._partition_combo,
            self._target_combo,
            self._filesystem_combo,
            self._mode_combo,
            self._persistence_toggle,
            self._persistence_size,
            self._persistence_unit,
            self._wtg_toggle,
            self._queue_list,
            self._queue_add_btn,
            self._queue_remove_btn,
            self._queue_clear_btn,
            self._flash_queue_btn,
            self._fleet_toggle,
            self._fleet_stop_btn,
        ]

        # Keep primary action states in sync with selections and busy state
        self._iso_zone.iso_selected.connect(self._on_iso_selected)
        self._iso_zone.hash_done.connect(self._on_iso_hash_ready)
        self._iso_zone.iso_analysis.connect(self._on_iso_analysis)
        self._update_controls_state()

        geometry = settings.get("window_geometry")
        if geometry:
            self.restoreGeometry(
                QByteArray.fromBase64(geometry.encode("ascii"))
            )
            self._clamp_to_screen()

        self._poller = DrivePoller(self._detector, 2000)
        self._poller.drives_ready.connect(self._on_drives_ready)
        self._poller.start()
        self._cancel_btn.setEnabled(False)
        self._wipe_btn.setEnabled(False)
        self._verify_toggle.setChecked(
            bool(settings.get("verify_after_write"))
        )
        self._verify_toggle.toggled.connect(self._update_verify_controls)
        self._update_verify_controls()
        self._dots_btn.clicked.connect(self._show_dots_menu)
        self._iso_zone._clear_guard = lambda: self._busy()
        self._iso_zone._browse_guard = lambda: self._busy()
        self._verify_zone._clear_guard = lambda: self._busy()
        QShortcut(QKeySequence("F5"), self).activated.connect(
            self._request_scan
        )
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(
            self._iso_zone._browse
        )
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(
            self._on_flash_clicked
        )
        for s in self.findChildren(QShortcut):
            s.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.setWindowIcon(self._make_flint_icon())
        self._setup_tray()

        # One-time onboarding modal to improve discoverability
        try:
            if not settings.get("onboarding_seen"):
                dialogs.inform(
                    self,
                    kind="info",
                    title="Welcome to Flint",
                    message=(
                        "Welcome — a quick tour:\n\n"
                        "1) Pick an image\n"
                        "2) Choose a target drive\n"
                        "3) Flash and optionally verify\n\n"
                        "Dangerous actions (wipe/flash) require typed "
                        "confirmation."
                    ),
                    check="Don't show this again",
                )
                settings.set_many(onboarding_seen=True)
        except Exception:
            pass

    def _content_minimum_width(self) -> int:
        """Smallest window width that fits every page without clipping.

        QScrollArea excludes its widget's minimum when widgetResizable is
        set, so the window's natural minimum ignores scroll-area content
        and the right edge gets cut off at the resizing floor. Compute
        the floor from the actual page layouts and the bottom bar."""
        needed = 0
        scrollbar = 18
        frame = 2
        for i in range(self._pages.count()):
            page = self._pages.widget(i)
            if page is None:
                continue
            lay = page.layout()
            if lay is not None:
                needed = max(needed, lay.minimumSize().width())
            for sa in page.findChildren(QScrollArea):
                inner = sa.widget()
                if inner is None:
                    continue
                inner_layout = inner.layout()
                if inner_layout is None:
                    continue
                needed = max(
                    needed,
                    inner_layout.minimumSize().width() + frame + scrollbar,
                )
        bar_layout = self._bottombar.layout()
        if bar_layout is not None:
            needed = max(needed, bar_layout.minimumSize().width())
        return needed + 201 + 26

    def _soften_minimum_widths(self) -> None:
        """Relax min-width contributions that clip in narrow windows.

        The main column narrows with the window, and the scroll areas
        disable horizontal scrolling, so long mono labels push the
        content's minimum width past the viewport and get cut off at the
        right border. Word-wrapping keeps the text intact while letting
        the minimum width collapse to the longest word.
        """
        wrap_labels = {
            "capLabel",
            "statCap",
            "progTitle",
            "doneSummary",
            "verifyHint",
            "dropError",
            "driveName",
            "driveSub",
            "verifyLabel",
        }
        for w in self.findChildren(QLabel):
            if w.objectName() in wrap_labels:
                w.setWordWrap(True)

    def _clamp_to_screen(self) -> None:
        """Keep the window inside the current screen's visible area.

        The saved window geometry can put the window partially or fully
        off-screen after a monitor change or resolution switch, which
        leaves the right side unreachable. Shrink to fit when possible,
        clamp the position to the edges, and center the window when it
        does not intersect any screen at all.
        """
        frame = self.frameGeometry()
        screen = QGuiApplication.screenAt(frame.center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        if not frame.intersects(available):
            self.move(
                available.center().x() - self.width() // 2,
                available.center().y() - self.height() // 2,
            )
            return
        if (
            frame.width() > available.width()
            and available.width() >= self.minimumWidth()
        ):
            self.resize(available.width(), self.height())
            frame = self.frameGeometry()
        if (
            frame.height() > available.height()
            and available.height() >= self.minimumHeight()
        ):
            self.resize(self.width(), available.height())
            frame = self.frameGeometry()
        x = min(
            max(frame.x(), available.left()),
            available.right() - frame.width(),
        )
        y = min(
            max(frame.y(), available.top()),
            available.bottom() - frame.height(),
        )
        self.move(x, y)

    def _busy(self) -> bool:
        return bool(
            self._writing
            or self._writer is not None
            or self._verifier is not None
            or self._page_verifier is not None
            or self._wipe_worker is not None
            or self._backup_worker is not None
            or self._clone_worker is not None
            or self._queue_active
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
        mark = QLabel()
        mark.setObjectName("logoMark")
        mark.setFixedSize(26, 26)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(self._make_flint_icon().pixmap(26, 26))
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
            ("Write", True, None),
            ("Verify", False, None),
            ("History", False, None),
            ("Settings", False, None),
        ]

        nav = QVBoxLayout()
        nav.setContentsMargins(8, 12, 8, 12)
        nav.setSpacing(1)
        self._nav_items: list[NavItem] = []
        for index, (text, active, badge) in enumerate(nav_items):
            item = NavItem(text, active, badge)
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
        row.addLayout(info, 1)
        return chip

    def _show_drive_picker(self) -> None:
        if self._busy():
            return
        menu = QMenu(self)
        if not self._drives:
            action = menu.addAction("No USB drive detected")
            assert action is not None
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
                letters = drive.get("letters") or (
                    [drive["letter"]] if drive.get("letter") else []
                )
                letter_label = (
                    ", ".join(f"{l}:" for l in letters)
                    if letters
                    else "no drive letter"
                )
                label = (
                    f"{drive['model'] or drive['name']} \u00b7 "
                    f"{size} ({letter_label})"
                )
                action = menu.addAction(label)
                assert action is not None
                if serial:
                    action.setToolTip(f"S/N {serial}")
                action.setCheckable(True)
                action.setChecked(
                    drive.get("physical_path") == selected_path
                )
                action.triggered.connect(
                    lambda _, d=drive: self._select_drive(d)
                )
        menu.addSeparator()
        backup_action = menu.addAction("Backup this drive to an image\u2026")
        assert backup_action is not None
        backup_action.setEnabled(self._current_drive is not None)
        backup_action.setToolTip(
            "Read the selected drive into a disk image file"
        )
        backup_action.triggered.connect(self._on_backup_clicked)
        clone_action = menu.addAction("Clone this drive to another\u2026")
        assert clone_action is not None
        clone_action.setEnabled(self._current_drive is not None)
        clone_action.setToolTip(
            "Copy the selected drive onto a second drive"
        )
        clone_action.triggered.connect(self._on_clone_clicked)
        menu.addSeparator()
        refresh = menu.addAction("\u21bb Refresh")
        assert refresh is not None
        refresh.triggered.connect(self._request_scan)
        menu.exec(QCursor.pos())

    def _select_drive(self, drive: dict[str, Any]) -> None:
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
            self._target_change.setText("Choose drive")
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
            self._target_change.setText("Change")
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
            letters = drive.get("letters") or (
                [drive["letter"]] if drive.get("letter") else []
            )
            letter_label = (
                ", ".join(f"{l}:" for l in letters)
                if letters
                else "no drive letter"
            )
            sub = f"{size} \u00b7 {letter_label}"
            if serial:
                sub += f" \u00b7 S/N \u2026{serial}"
            self._drive_sub.setText(sub)
            detail = f"{name} \u00b7 {size} \u00b7 {letter_label}"
            if serial:
                detail += f" \u00b7 S/N \u2026{serial}"
            self._target_detail.setText(detail)
            self._target_change.setVisible(True)
            self._target_admin_btn.setVisible(False)
            self._subtitle.setText(f"{name} \u00b7 {size}")
            if hasattr(self, "_verify_target"):
                self._verify_target.setText(
                    f"Target: {name} \u00b7 {letter_label}"
                )
        for widget in (
            self._chip_dot,
            self._drive_name,
            self._drive_sub,
            self._target_detail,
        ):
            _restyle(widget)

    @staticmethod
    def _serial_tail(drive: dict[str, Any]) -> str | None:
        serial = (drive.get("serial") or "").strip()
        if len(serial) >= 4:
            return serial[-4:]
        return serial or None

    @staticmethod
    def _is_elevated() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
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
        if not dialogs.confirm(
            self,
            kind="info",
            title="Flint \u2014 elevation required",
            message=(
                "Flint needs administrator privileges to access raw disks.\n\n"
                "Elevating will restart the application with elevated rights."
            ),
            accept="Elevate",
            reject="Continue without elevation",
        ):
            logger.info("user chose to continue without elevation from UI")
            return
        try:
            # Release the single-instance pipe BEFORE spawning: the new
            # elevated process must be able to acquire it while this one is
            # still shutting down.
            from main import release_single_instance

            release_single_instance()
        except Exception:
            logger.exception("failed to release single-instance lock")
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", cmd, args, None, 5
            )
        except Exception:
            logger.exception("ShellExecuteW failed during relaunch")
            result = 0
        if result <= 32:
            self._progress.set_error(
                "Windows declined to run Flint elevated"
            )
            return
        self._save_settings()
        self._shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _drive_content_summary(self, drive: dict[str, Any] | None) -> str | None:
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
        self, drive: dict[str, Any] | None, iso_path: str | None = None
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
        self, drive: dict[str, Any] | None, iso_path: str | None = None
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

        text, ok = dialogs.input_text(
            self,
            title="Confirm destructive action",
            message=prompt,
        )
        if not ok:
            return False
        entered = text.strip()
        # match case-insensitively for names; exact match for serial tail
        if serial and len(serial) >= 4:
            return entered == want
        return entered.lower() == want.lower()

    def _on_drives_ready(self, drives: list[dict[str, Any]]) -> None:
        if self._busy():
            return
        self._drives = drives
        self._fleet_tick()
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
        self._update_controls_state()

    def _request_scan(self) -> None:
        if self._busy():
            return
        self._poller.request_scan()

    def _on_iso_selected(self, path: str) -> None:
        # Called when an ISO is selected; refresh control states
        self._sidecar_status, self._sidecar_detail = checksum_mod.check_sidecar(
            path, self._iso_zone.digest
        )
        self._update_sidecar_label()
        try:
            self._update_controls_state()
        except Exception:
            pass

    def _on_iso_hash_ready(self, path: str, ok: bool, digest: str) -> None:
        # The drop zone finished hashing the image: re-evaluate any sidecar.
        if path != getattr(self._iso_zone, "path", None):
            return
        self._sidecar_status, self._sidecar_detail = checksum_mod.check_sidecar(
            path, digest if ok else None
        )
        self._update_sidecar_label()

    def _update_sidecar_label(self) -> None:
        status = self._sidecar_status
        name = self._sidecar_detail or ""
        if status == "missing":
            self._sidecar_label.setVisible(False)
            self._sidecar_label.setText("")
            return
        if status == "pending":
            self._sidecar_label.setProperty("error", False)
            self._sidecar_label.setText(
                f"Checksum {name} found \u2014 verifying image\u2026"
            )
        elif status == "ok":
            self._sidecar_label.setProperty("error", False)
            self._sidecar_label.setText(
                f"SHA-256 checksum matches {name}"
            )
        elif status == "mismatch":
            self._sidecar_label.setProperty("error", True)
            self._sidecar_label.setText(
                f"SHA-256 MISMATCH vs {name} \u2014 the image is corrupt "
                "or wrong. Flashing is blocked."
            )
        else:
            self._sidecar_label.setProperty("error", True)
            self._sidecar_label.setText(
                f"Checksum {name or 'sidecar'} could not be read \u2014 "
                "flashing is blocked until it is removed or fixed."
            )
        self._sidecar_label.setVisible(True)
        _restyle(self._sidecar_label)

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
        self._pages.addWidget(self._build_settings_page())
        layout.addWidget(self._pages, 1)

        bottom_divider = QFrame()
        bottom_divider.setObjectName("hdiv")
        bottom_divider.setFixedHeight(1)
        layout.addWidget(bottom_divider)

        self._bottombar = self._build_bottombar()
        layout.addWidget(self._bottombar)
        return main

    def _build_dots_menu(self) -> QMenu:
        menu = QMenu(self)
        settings_action = menu.addAction("Settings")
        assert settings_action is not None
        settings_action.triggered.connect(lambda: self._on_nav_clicked(3))
        menu.addSeparator()
        update_action = menu.addAction("Check for updates\u2026")
        assert update_action is not None
        update_action.setEnabled(self._update_checker is None)
        update_action.triggered.connect(self._on_check_updates_clicked)
        return menu

    def _show_dots_menu(self) -> None:
        self._build_dots_menu().exec(QCursor.pos())

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(24, 20, 24, 20)
        col.setSpacing(14)

        col.addWidget(self._build_section_label("Settings"))

        appearance = QFrame()
        appearance.setObjectName("block")
        acol = QVBoxLayout(appearance)
        acol.setContentsMargins(14, 12, 14, 12)
        acol.setSpacing(8)

        title = QLabel("APPEARANCE")
        title.setObjectName("capLabel")
        title.setProperty("colorRole", "muted")
        acol.addWidget(title)

        self._theme_radios: dict[str, QRadioButton] = {}
        theme = settings.get("theme")
        for label, key in (
            ("Light theme", "light"),
            ("High contrast", "high-contrast"),
            ("Dark theme", "dark"),
        ):
            radio = QRadioButton(label)
            radio.setChecked(theme == key)
            radio.toggled.connect(
                lambda checked, t=key: (
                    self._set_theme(t) if checked else None
                )
            )
            self._theme_radios[key] = radio
            acol.addWidget(radio)

        behavior = QFrame()
        behavior.setObjectName("block")
        bcol = QVBoxLayout(behavior)
        bcol.setContentsMargins(14, 12, 14, 12)
        bcol.setSpacing(8)

        title = QLabel("BEHAVIOR")
        title.setObjectName("capLabel")
        title.setProperty("colorRole", "muted")
        bcol.addWidget(title)

        self._settings_expert_toggle = ToggleSwitch(
            checked=bool(settings.get("expert_mode"))
        )
        self._settings_expert_toggle.toggled.connect(self._set_expert_mode)
        expert_row = QHBoxLayout()
        expert_row.setSpacing(8)
        expert_label = QLabel("Expert mode")
        expert_label.setObjectName("capLabel")
        expert_label.setProperty("colorRole", "label")
        expert_row.addWidget(self._settings_expert_toggle)
        expert_row.addWidget(expert_label)
        expert_row.addStretch()
        bcol.addLayout(expert_row)

        self._close_to_tray_toggle = ToggleSwitch(
            checked=bool(settings.get("close_to_tray"))
        )
        self._close_to_tray_toggle.toggled.connect(
            lambda on: settings.set_many(close_to_tray=bool(on))
        )
        tray_row = QHBoxLayout()
        tray_row.setSpacing(8)
        tray_label = QLabel(
            "Minimize to system tray when the app is closed"
        )
        tray_label.setObjectName("capLabel")
        tray_label.setProperty("colorRole", "label")
        tray_label.setWordWrap(True)
        tray_row.addWidget(self._close_to_tray_toggle)
        tray_row.addWidget(tray_label)
        tray_row.addStretch()
        bcol.addLayout(tray_row)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._close_to_tray_toggle.setEnabled(False)
            tray_label.setToolTip(
                "No system tray is available on this session \u2014 "
                "closing the window will always quit the app."
            )

        actions = QFrame()
        actions.setObjectName("block")
        xcol = QVBoxLayout(actions)
        xcol.setContentsMargins(14, 12, 14, 12)
        xcol.setSpacing(8)

        title = QLabel("WINDOW")
        title.setObjectName("capLabel")
        title.setProperty("colorRole", "muted")
        xcol.addWidget(title)

        reset_btn = QPushButton("Reset window size")
        reset_btn.setObjectName("ghost")
        reset_btn.clicked.connect(lambda: self.resize(900, 580))
        xcol.addWidget(reset_btn)

        col.addWidget(appearance)
        col.addWidget(behavior)
        col.addWidget(actions)
        col.addStretch()
        return page

    def _set_theme(self, theme: str) -> None:
        from ui.style import build_style

        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(build_style(theme))
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
        self._history_empty.setWordWrap(True)
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
        diag_btn = QPushButton("Export diagnostics\u2026")
        diag_btn.clicked.connect(self._on_export_diagnostics)
        diag_btn.setToolTip(
            "Bundle version info, drive list, history and logs into a "
            "single text file to attach to a bug report."
        )
        buttons.addWidget(export_btn)
        buttons.addWidget(import_btn)
        buttons.addWidget(clear_btn)
        buttons.addStretch()
        buttons.addWidget(diag_btn)
        col.addLayout(buttons)
        return page

    def _on_export_diagnostics(self) -> None:
        from core.diagnostics import build_diagnostics

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export diagnostics",
            os.path.join(
                settings.get("last_iso_dir") or "",
                f"flint-diagnostics-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.txt",
            ),
            "Text (*.txt)",
        )
        if not target:
            return
        settings.set_many(last_iso_dir=os.path.dirname(target))
        try:
            drives = self._detector.list_removable_drives()
            text = build_diagnostics(
                drives,
                elevated=self._is_elevated(),
                entries=load_history(),
            )
            Path(target).write_text(text, encoding="utf-8")
            self._show_tray_notify("Diagnostics", "Exported.")
        except Exception:
            logger.exception("export diagnostics failed")
            dialogs.inform(
                self,
                kind="error",
                title="Export diagnostics",
                message="Could not write the diagnostics file.",
            )

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
            dialogs.inform(
                self,
                kind="error",
                title="Export",
                message="Could not write the file.",
            )

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
        if existing > 0 and not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 import history?",
            message=(
                f"Importing will replace your {existing} existing "
                f"entr{'y' if existing == 1 else 'ies'}."
            ),
            accept="Replace history",
            accept_style="danger",
        ):
            return
        ok, _count = import_history(source)
        if ok:
            self._reload_history()
            self._show_from_tray()
        else:
            dialogs.inform(
                self,
                kind="error",
                title="Import",
                message="That file is not a valid Flint history.",
            )

    def _on_history_clear(self) -> None:
        if not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 clear history?",
            message="Remove all flash history entries?",
            accept="Clear",
            accept_style="danger",
        ):
            return
        clear_history()
        self._reload_history()

    def _build_verify_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

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
        hint.setWordWrap(True)
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

        scroll.setWidget(page)
        return scroll

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
                f"Comparing against: {os.path.basename(iso or '')}"
            )
        verifier = VerifyWorker(drive_path, expected, size)
        self._page_verifier = verifier
        self._poller.suspend()
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
        self._poller.resume()
        self._verify_start_btn.setEnabled(True)
        self._verify_cancel_btn.setEnabled(False)
        self._set_taskbar_progress(None)
        self._verify_mode.setText("")
        if ok:
            self._verify_progress.set_done()
            self._verify_progress._title.setText("Verified")
            dialogs.completion(
                self,
                kind="success",
                title="Verification passed",
                message=message or "The drive matches the selected image.",
                buttons=[("Close", "primary", "close")],
            )
        else:
            self._verify_progress.set_error(
                self._friendly_error(message or "Verification failed")
            )
            dialogs.completion(
                self,
                kind="error",
                title="Verification failed",
                message=self._friendly_error(message or "Verification failed"),
                buttons=[("Close", "primary", "close")],
            )

    def _on_page_verify_cancel(self) -> None:
        if self._page_verifier is not None:
            self._page_verifier.cancel()

    def _on_nav_clicked(self, index: int) -> None:
        if self._busy():
            return
        for i, item in enumerate(self._nav_items):
            item.set_active(i == index)
        page = {0: 0, 1: 2, 2: 1, 3: 3}[index]
        if page == 1:
            self._reload_history()
        self._pages.setCurrentIndex(page)
        self._bottombar.setVisible(page == 0)

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
            marker = "\u2713\ufe0e" if entry.get("success") else "\u2715"
            item = QListWidgetItem(
                f"{marker}  {entry.get('iso', '?')}  \u2192  "
                f"{entry.get('drive', '?')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, entry)
            try:
                duration = float(entry.get("duration", 0) or 0)
            except (TypeError, ValueError):
                # Hand-edited or imported history can carry junk durations;
                # never crash the page render over one entry.
                duration = 0.0
            verified = "Yes" if entry.get("verified") else "No"
            outcome = (
                "\u2713\ufe0e complete" if entry.get("success") else "\u2715 failed"
            )
            item.setToolTip(
                f"{entry.get('timestamp', '')}\n"
                f"Duration: {round(duration)} s\n"
                f"Verification: {verified}\n"
                f"Outcome: {outcome}"
            )
            self._history_list.addItem(item)

    def _on_history_activated(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        report_text = self._build_report_text(entry)
        dlg = dialogs.FlintDialog(
            self,
            kind="info",
            title="Flash report",
            message=report_text,
            buttons=[
                ("Copy", "ghost", "copy"),
                ("Close", "primary", "close"),
            ],
            mono=True,
        )
        if dlg.run() == "copy":
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(report_text)
            if self._tray is not None:
                self._tray.showMessage(
                    "Flint",
                    "Flash report copied to clipboard.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )

    def _make_flint_icon(self) -> QIcon:
        candidates: list[str] = []
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", "")
            if meipass:
                candidates.append(os.path.join(meipass, "ui", "flint.ico"))
        else:
            candidates.append(
                str(Path(__file__).resolve().parent / "flint.ico")
            )
        for candidate in candidates:
            if os.path.isfile(candidate):
                return QIcon(candidate)
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
            painter.drawPolygon(QPolygonF(points))
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
        assert show_action is not None
        show_action.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        assert quit_action is not None
        quit_action.triggered.connect(self._on_tray_quit)
        self._tray.setContextMenu(menu)
        self._tray.show()

    def _show_toast(self, title: str, message: str) -> None:
        """Show a Windows toast notification using PowerShell."""
        try:
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, "
                "Windows.UI.Notifications, ContentType = WindowsRuntime] "
                "| Out-Null\n"
                "[Windows.Data.Xml.Dom.XmlDocument, "
                "Windows.Data.Xml.Dom, ContentType = WindowsRuntime] "
                "| Out-Null\n"
                f'$template = "<toast><visual><binding '
                f"template='ToastGeneric'><text>{title}</text>"
                f"<text>{message}</text></binding></visual></toast>\"\n"
                "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
                "$xml.LoadXml($template)\n"
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
                "[Windows.UI.Notifications.ToastNotificationManager]"
                "::CreateToastNotifier('Flint').Show($toast)"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except Exception:
            pass

    def _on_tray_quit(self) -> None:
        if self._busy() and not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 still writing",
            message=(
                "A write or verification is still in progress.\n"
                "Quitting now may leave the drive unusable."
            ),
            accept="Quit anyway",
            accept_style="danger",
        ):
            return
        self._save_settings()
        self._shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

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

            def _vfn(index: int, *argtypes: Any) -> Any | None:
                # Indexing the vtable pointer yields the raw slot address as
                # an int (it is POINTER(c_void_p)); c_void_p instances are
                # returned only by functions, not by pointer indexing.
                slot = vtbl[index]
                if not slot:
                    return None
                return ctypes.WINFUNCTYPE(ctypes.HRESULT, *argtypes)(int(slot))

            init = _vfn(3, ctypes.c_void_p)
            set_value = _vfn(
                9,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_ulonglong,
                ctypes.c_ulonglong,
            )
            set_state = _vfn(
                10, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int
            )
            if init is None or set_value is None or set_state is None:
                return
            if init(ptr) != 0:
                return
            self._tb = (ptr, set_value, set_state)
        except Exception:
            logger.exception("_init_taskbar failed to initialize COM taskbar integration")
            self._tb = None

    def _set_taskbar_progress(
        self, percent: float | None, error: bool = False
    ) -> None:
        if self._tb is None:
            # A failed first attempt (e.g. Explorer not ready yet) must not
            # latch forever: retry at most every 5 seconds.
            now = time.monotonic()
            if self._tb_last_try and now - self._tb_last_try < 5:
                return
            self._tb_last_try = now
            self._init_taskbar()
            if self._tb is None:
                return
        ptr, set_value, set_state = self._tb
        hwnd = ctypes.c_void_p(0)
        try:
            hwnd = ctypes.c_void_p(int(self.winId()))
        except RuntimeError:
            return
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

    def changeEvent(self, event: QEvent | None) -> None:
        assert event is not None
        if event.type() == QEvent.Type.WindowStateChange:
            self._lifecycle_log(
                f"changeEvent minimized={self.isMinimized()} "
                f"visible={self.isVisible()} writing={self._writing}"
            )
        super().changeEvent(event)

    def event(self, e: QEvent | None) -> bool:
        assert e is not None
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
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._poller.requestInterruption()
        active = (
            self._writer,
            self._verifier,
            self._page_verifier,
            self._wipe_worker,
            self._backup_worker,
            self._clone_worker,
        )
        # Cancel and wait for every live worker with the event loop pumping.
        # Never destroy a running QThread (that aborts the process and
        # abandons the drive mid-write); if a worker is still alive after the
        # grace period, keep a reference so it is not garbage-collected and
        # let the process exit without touching it.
        for worker in active:
            if worker is not None and worker.isRunning():
                worker.cancel()
        deadline = time.monotonic() + 30.0
        while any(
            w is not None and w.isRunning() for w in active
        ):
            for worker in active:
                if worker is not None and worker.isRunning():
                    worker.wait(100)
            QApplication.processEvents()
            if time.monotonic() > deadline:
                break
        for worker in active:
            if worker is not None and worker.isRunning():
                self._zombies.append(worker)
            elif worker is not None:
                self._retire(worker)
        if not self._poller.wait(2000):
            self._zombies.append(self._poller)
        for retired in self._retired_workers:
            if retired is not None and not retired.isRunning():
                retired.deleteLater()
        self._retired_workers.clear()

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        assert event is not None
        mime = event.mimeData()
        if mime is None:
            event.ignore()
            return
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".sha256"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent | None) -> None:
        assert event is not None
        mime = event.mimeData()
        if mime is None:
            return
        for url in mime.urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".sha256"):
                self._load_sha256_file(url.toLocalFile())
                event.acceptProposedAction()
                return
        event.ignore()

    def _load_sha256_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(256).strip().lower()
            if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
                self._on_nav_clicked(1)
                self._verify_sha_input.setText(text)
        except OSError:
            pass

    def closeEvent(self, event: QCloseEvent | None) -> None:
        assert event is not None
        self._lifecycle_log(
            f"closeEvent writing={self._writing} "
            f"visible={self.isVisible()}"
        )
        if self._busy():
            event.ignore()
            if self._tray is not None:
                self._tray.showMessage(
                    "Flint",
                    "A write is in progress \u2014 closing is disabled.\n"
                    "You can minimise the window to the taskbar.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            else:
                dialogs.inform(
                    self,
                    kind="warning",
                    title="Flint \u2014 write in progress",
                    message=(
                        "A write is in progress \u2014 closing is disabled.\n"
                        "You can minimise the window to the taskbar."
                    ),
                )
            return
        self._save_settings()
        if self._tray is not None and settings.get("close_to_tray"):
            event.ignore()
            self.hide()
            return
        self._shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()
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
        self._wipe_btn.clicked.connect(lambda: self._on_wipe_clicked("zero"))
        wipe_menu = QMenu(self)
        for label, method, tip in (
            ("Zero fill (fast)", "zero", "Single pass of zeros"),
            (
                "Random data (NIST)",
                "nist",
                "Single pass of random data",
            ),
            (
                "DoD 5220.22-M (3 passes)",
                "dod",
                "Zeros, ones, then random data",
            ),
        ):
            action = wipe_menu.addAction(label)
            assert action is not None
            action.setToolTip(tip)
            action.triggered.connect(
                lambda _, m=method: self._on_wipe_clicked(m)
            )
        self._wipe_menu = wipe_menu
        self._wipe_menu_btn = QPushButton("\u25be")
        self._wipe_menu_btn.setObjectName("iconBtn")
        self._wipe_menu_btn.setToolTip("Wipe method")
        self._wipe_menu_btn.setFixedSize(30, 30)
        self._wipe_menu_btn.clicked.connect(
            lambda: wipe_menu.exec(QCursor.pos())
        )

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
        row.addWidget(self._wipe_menu_btn)
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
        dots.setToolTip("Open settings")
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
        self._sidecar_label = QLabel("")
        self._sidecar_label.setObjectName("capLabel")
        self._sidecar_label.setWordWrap(True)
        self._sidecar_label.setVisible(False)
        col.addWidget(self._sidecar_label)

        hint = QLabel(
            "The image is written to the drive as-is (raw image mode)."
        )
        hint.setObjectName("capLabel")
        hint.setProperty("colorRole", "muted")
        hint.setWordWrap(True)
        self._raw_hint = hint
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
        self._target_change = QPushButton("Choose drive")
        self._target_change.setObjectName("primary")
        self._target_change.setMinimumHeight(
            style.DESIGN_TOKENS["button_height"]
        )
        self._target_change.clicked.connect(self._show_drive_picker)
        target_row.addWidget(self._target_change)

        self._target_admin_btn = QPushButton("Run as administrator")
        self._target_admin_btn.setObjectName("ghost")
        self._target_admin_btn.clicked.connect(self._relaunch_elevated)
        self._target_admin_btn.setVisible(False)
        target_row.addWidget(self._target_admin_btn)

        col.addWidget(self._build_expert_options())

        col.addWidget(self._build_verify_options())

        queue_block = QFrame()
        queue_block.setObjectName("block")
        queue_col = QVBoxLayout(queue_block)
        queue_col.setContentsMargins(14, 12, 14, 12)
        queue_col.setSpacing(8)
        queue_header = QHBoxLayout()
        queue_title = QLabel("FLASH QUEUE")
        queue_title.setObjectName("capLabel")
        queue_title.setProperty("colorRole", "muted")
        queue_hint = QLabel(
            "Flash several images to the same drive, one after another"
        )
        queue_hint.setObjectName("capLabel")
        queue_hint.setProperty("colorRole", "muted")
        queue_hint.setWordWrap(True)
        queue_header.addWidget(queue_title)
        queue_header.addStretch()
        queue_header.addWidget(queue_hint)
        queue_col.addLayout(queue_header)
        self._queue_list = QListWidget()
        self._queue_list.setFixedHeight(92)
        queue_col.addWidget(self._queue_list)
        queue_buttons = QHBoxLayout()
        self._queue_add_btn = QPushButton("Add images\u2026")
        self._queue_add_btn.setObjectName("ghost")
        self._queue_add_btn.clicked.connect(self._on_queue_add_clicked)
        self._queue_remove_btn = QPushButton("Remove selected")
        self._queue_remove_btn.setObjectName("ghost")
        self._queue_remove_btn.clicked.connect(self._on_queue_remove_clicked)
        self._queue_clear_btn = QPushButton("Clear")
        self._queue_clear_btn.setObjectName("ghost")
        self._queue_clear_btn.clicked.connect(self._on_queue_clear_clicked)
        self._flash_queue_btn = QPushButton("Flash queue")
        self._flash_queue_btn.setObjectName("primary")
        self._flash_queue_btn.setMinimumHeight(
            style.DESIGN_TOKENS["button_height"]
        )
        self._flash_queue_btn.clicked.connect(self._on_flash_queue_clicked)
        queue_buttons.addWidget(self._queue_add_btn)
        queue_buttons.addWidget(self._queue_remove_btn)
        queue_buttons.addWidget(self._queue_clear_btn)
        queue_buttons.addStretch()
        queue_buttons.addWidget(self._flash_queue_btn)
        queue_col.addLayout(queue_buttons)

        fleet_row = QHBoxLayout()
        fleet_row.setSpacing(8)
        self._fleet_toggle = ToggleSwitch(False)
        self._fleet_toggle.setObjectName("fleetToggle")
        self._fleet_toggle.toggled.connect(self._on_fleet_toggled)
        fleet_label = QLabel("Fleet mode")
        fleet_label.setObjectName("capLabel")
        fleet_label.setProperty("colorRole", "muted")
        fleet_hint = QLabel(
            "write the queue to every drive you plug in, one after another"
        )
        fleet_hint.setObjectName("capLabel")
        fleet_hint.setProperty("colorRole", "muted")
        fleet_hint.setWordWrap(True)
        fleet_row.addWidget(self._fleet_toggle)
        fleet_row.addWidget(fleet_label)
        fleet_row.addWidget(fleet_hint, 1)
        queue_col.addLayout(fleet_row)

        skip_row = QHBoxLayout()
        skip_row.setSpacing(8)
        self._fleet_skip_flashed = ToggleSwitch(False)
        self._fleet_skip_flashed.setObjectName("fleetToggle")
        skip_label = QLabel("Skip already-flashed drives")
        skip_label.setObjectName("capLabel")
        skip_label.setProperty("colorRole", "muted")
        skip_row.addWidget(self._fleet_skip_flashed)
        skip_row.addWidget(skip_label)
        skip_row.addStretch(1)
        queue_col.addLayout(skip_row)

        self._fleet_banner = QFrame()
        self._fleet_banner.setObjectName("block")
        self._fleet_banner.setVisible(False)
        fleet_banner_row = QHBoxLayout(self._fleet_banner)
        fleet_banner_row.setContentsMargins(12, 8, 12, 8)
        fleet_banner_row.setSpacing(6)
        self._fleet_label = QLabel("")
        self._fleet_label.setObjectName("fleetLabel")
        self._fleet_label.setProperty("colorRole", "label")
        self._fleet_label.setWordWrap(True)
        self._fleet_stop_btn = QPushButton("Stop")
        self._fleet_stop_btn.setObjectName("ghost")
        self._fleet_stop_btn.clicked.connect(self._on_fleet_stop_clicked)
        fleet_banner_row.addWidget(self._fleet_label, 1)
        fleet_banner_row.addWidget(self._fleet_stop_btn)
        queue_col.addWidget(self._fleet_banner)

        self._queue_block = queue_block
        col.addWidget(queue_block)

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
        steps.setWordWrap(True)
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

    def _build_expert_options(self) -> QFrame:
        """Advanced options (partition scheme / target / filesystem / mode).

        Visible only when expert mode is enabled; the toggle and the choices
        persist in settings.
        """
        card = QFrame()
        card.setObjectName("block")
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(7)
        self._expert_toggle = ToggleSwitch(
            checked=bool(settings.get("expert_mode"))
        )
        self._expert_toggle.setToolTip(
            "Show partition scheme, target system, filesystem and write mode"
        )
        expert_label = QLabel("Expert options")
        expert_label.setObjectName("capLabel")
        expert_label.setProperty("colorRole", "label")
        toggle_row.addWidget(self._expert_toggle)
        toggle_row.addWidget(expert_label)
        toggle_row.addStretch()
        col.addLayout(toggle_row)

        body = QWidget()
        body_col = QVBoxLayout(body)
        body_col.setContentsMargins(0, 0, 0, 0)
        body_col.setSpacing(8)
        col.addWidget(body)

        self._partition_combo = QComboBox()
        self._partition_combo.addItem("Auto (GPT for UEFI, MBR for legacy)", "auto")
        self._partition_combo.addItem("GPT", "gpt")
        self._partition_combo.addItem("MBR", "mbr")
        self._target_combo = QComboBox()
        self._target_combo.addItem("Auto", "auto")
        self._target_combo.addItem("UEFI", "uefi")
        self._target_combo.addItem("Legacy", "legacy")
        self._filesystem_combo = QComboBox()
        self._filesystem_combo.addItem("FAT32", "fat32")
        self._filesystem_combo.addItem("NTFS", "ntfs")
        self._filesystem_combo.addItem("exFAT", "exfat")
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Auto (raw write)", "auto")
        self._mode_combo.addItem("Raw (DD)", "dd")
        self._mode_combo.addItem("File copy", "filecopy")
        self._buffer_combo = QComboBox()
        for mb in (4, 8, 16, 32, 64):
            self._buffer_combo.addItem(f"{mb} MiB", mb)

        _help_text = {
            "partition_scheme": "Partition scheme",
            "target_system": "Target system",
            "filesystem": "Filesystem",
            "write_mode": "Write mode",
            "chunk_size_mb": "Buffer size",
        }
        for combo, key in (
            (self._partition_combo, "partition_scheme"),
            (self._target_combo, "target_system"),
            (self._filesystem_combo, "filesystem"),
            (self._mode_combo, "write_mode"),
            (self._buffer_combo, "chunk_size_mb"),
        ):
            # Long items (e.g. "Auto (GPT for UEFI, MBR for legacy)")
            # must not pin the window's minimum width; the combo still
            # expands to fill its row when space allows.
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            value = settings.get(key)
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(self._on_expert_changed)
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(_help_text[key])
            label.setObjectName("capLabel")
            label.setProperty("colorRole", "label")
            row.addWidget(label)
            row.addWidget(
                self._help_button(_HELP_TIPS[key])
            )
            row.addWidget(combo, 1)
            body_col.addLayout(row)

        self._native_toggle = ToggleSwitch(
            checked=bool(settings.get("native_writer"))
        )
        self._native_toggle.setToolTip(
            "Use the compiled native writer for raw writes "
            "(CreateFile/WriteFile with FILE_FLAG_NO_BUFFERING and aligned "
            "buffers); falls back to the Python writer when the extension "
            "is not built"
        )
        native_row = QHBoxLayout()
        native_row.setSpacing(8)
        native_label = QLabel("Native writer")
        native_label.setObjectName("capLabel")
        native_label.setProperty("colorRole", "label")
        native_row.addWidget(self._native_toggle)
        native_row.addWidget(native_label)
        native_row.addWidget(
            self._help_button(_HELP_TIPS["chunk_size_mb"])
        )
        native_row.addStretch()
        body_col.addLayout(native_row)
        self._native_toggle.toggled.connect(self._on_expert_changed)

        self._persistence_toggle = ToggleSwitch(checked=False)
        self._persistence_toggle.setToolTip(
            "Keep changes across reboots (Ubuntu casper-rw / Debian live)"
        )
        self._persistence_size = QLineEdit()
        self._persistence_size.setObjectName("persistenceSize")
        self._persistence_size.setPlaceholderText("1024")
        self._persistence_size.setFixedWidth(80)
        self._persistence_unit = QComboBox()
        self._persistence_unit.addItem("MB", "mb")
        self._persistence_unit.addItem("GB", "gb")
        persistence_row = QHBoxLayout()
        persistence_row.setSpacing(8)
        p_label = QLabel("Enable persistence")
        p_label.setObjectName("capLabel")
        p_label.setProperty("colorRole", "label")
        persistence_row.addWidget(self._persistence_toggle)
        persistence_row.addWidget(p_label)
        persistence_row.addWidget(
            self._help_button(_HELP_TIPS["persistence"])
        )
        persistence_row.addStretch()
        persistence_row.addWidget(self._persistence_size)
        persistence_row.addWidget(self._persistence_unit)
        body_col.addLayout(persistence_row)
        self._persistence_row_widgets = [
            self._persistence_toggle,
            self._persistence_size,
            self._persistence_unit,
        ]

        self._wtg_toggle = ToggleSwitch(checked=False)
        self._wtg_toggle.setToolTip(
            "Apply the Windows image with dism and boot it from USB "
            "(requires NTFS, file-copy mode)"
        )
        wtg_row = QHBoxLayout()
        wtg_row.setSpacing(8)
        wtg_label = QLabel("Windows To Go")
        wtg_label.setObjectName("capLabel")
        wtg_label.setProperty("colorRole", "label")
        wtg_row.addWidget(self._wtg_toggle)
        wtg_row.addWidget(wtg_label)
        wtg_row.addWidget(
            self._help_button(_HELP_TIPS["windows_to_go"])
        )
        wtg_row.addStretch()
        body_col.addLayout(wtg_row)
        self._wtg_row = wtg_row

        self._persistence_toggle.toggled.connect(self._on_expert_changed)
        self._wtg_toggle.toggled.connect(self._on_wtg_changed)
        self._expert_toggle.toggled.connect(self._set_expert_mode)
        self._expert_options_body = body
        self._expert_options_body.setVisible(
            bool(settings.get("expert_mode"))
        )
        self._update_expert_visibility()
        self._update_expert_hint()
        return card

    def _build_verify_options(self) -> QFrame:
        """Post-write verification options (SHA-256 compare + bad-block scan).

        Enabled whenever "Verify after write" is checked; choices persist in
        settings.
        """
        card = QFrame()
        card.setObjectName("block")
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)

        self._verify_sha_toggle = ToggleSwitch(
            checked=bool(settings.get("verify_sha256"))
        )
        self._verify_sha_toggle.setToolTip(
            "Read the drive back after writing and compare its SHA-256 "
            "against the image; mismatched regions are reported with offsets"
        )
        sha_row = QHBoxLayout()
        sha_row.setSpacing(8)
        sha_label = QLabel("Verify using SHA256")
        sha_label.setObjectName("capLabel")
        sha_label.setProperty("colorRole", "label")
        sha_row.addWidget(self._verify_sha_toggle)
        sha_row.addWidget(sha_label)
        sha_row.addWidget(
            self._help_button(_HELP_TIPS["verify_sha256"])
        )
        sha_row.addStretch()
        col.addLayout(sha_row)

        self._bad_block_toggle = ToggleSwitch(
            checked=bool(settings.get("bad_block_scan"))
        )
        self._bad_block_toggle.setToolTip(
            "Scan the drive for unreadable sectors; failed reads are retried "
            "and the failing offsets are reported"
        )
        self._bad_retries_input = QLineEdit()
        self._bad_retries_input.setObjectName("persistenceSize")
        self._bad_retries_input.setPlaceholderText("3")
        self._bad_retries_input.setFixedWidth(40)
        bad_row = QHBoxLayout()
        bad_row.setSpacing(8)
        bad_label = QLabel("Bad-block scan")
        bad_label.setObjectName("capLabel")
        bad_label.setProperty("colorRole", "label")
        bad_retries_label = QLabel("retries")
        bad_retries_label.setObjectName("capLabel")
        bad_retries_label.setProperty("colorRole", "label")
        bad_row.addWidget(self._bad_block_toggle)
        bad_row.addWidget(bad_label)
        bad_row.addWidget(
            self._help_button(_HELP_TIPS["bad_block_scan"])
        )
        bad_row.addStretch()
        bad_row.addWidget(self._bad_retries_input)
        bad_row.addWidget(bad_retries_label)
        col.addLayout(bad_row)

        self._verify_sha_toggle.toggled.connect(self._on_verify_options_changed)
        self._bad_block_toggle.toggled.connect(self._on_verify_options_changed)
        self._bad_retries_input.editingFinished.connect(
            self._on_verify_options_changed
        )
        self._verify_options_card = card
        return card

    def _on_verify_options_changed(self) -> None:
        retries = self._bad_block_retries_value()
        self._bad_retries_input.setText(str(retries))
        settings.set_many(
            verify_sha256=self._verify_sha_toggle.isChecked(),
            bad_block_scan=self._bad_block_toggle.isChecked(),
            bad_block_retries=retries,
        )
        self._update_verify_controls()

    def _bad_block_retries_value(self) -> int:
        raw = self._bad_retries_input.text().strip() or "3"
        try:
            return max(1, min(10, int(raw)))
        except ValueError:
            return 3

    def _update_verify_controls(self) -> None:
        enabled = self._verify_toggle.isChecked()
        self._verify_sha_toggle.setEnabled(enabled)
        self._bad_block_toggle.setEnabled(enabled)
        self._bad_retries_input.setEnabled(
            enabled and self._bad_block_toggle.isChecked()
        )

    def _on_expert_changed(self) -> None:
        settings.set_many(
            partition_scheme=self._partition_combo.currentData(),
            target_system=self._target_combo.currentData(),
            filesystem=self._filesystem_combo.currentData(),
            write_mode=self._mode_combo.currentData(),
            chunk_size_mb=self._buffer_combo.currentData(),
            native_writer=self._native_toggle.isChecked(),
        )
        self._update_expert_visibility()
        self._update_expert_hint()

    def _on_wtg_changed(self, enabled: bool) -> None:
        self._on_expert_changed()
        if enabled:
            # Windows To Go requires an NTFS target; keep the choice honest.
            self._filesystem_combo.setCurrentIndex(
                self._filesystem_combo.findData("ntfs")
            )
            self._filesystem_combo.setEnabled(False)
            self._persistence_toggle.setChecked(False)
            self._persistence_toggle.setEnabled(False)
        else:
            self._filesystem_combo.setEnabled(True)
            self._persistence_toggle.setEnabled(True)

    def _on_iso_analysis(
        self,
        path: str,
        is_linux: bool,
        is_windows: bool,
        is_hybrid: bool,
    ) -> None:
        if path and path != self._iso_zone.path:
            return
        self._iso_linux = is_linux
        self._iso_windows = is_windows
        self._iso_hybrid = is_hybrid
        if not is_linux:
            self._persistence_toggle.setChecked(False)
        if not is_windows:
            self._wtg_toggle.setChecked(False)
        self._update_expert_visibility()
        self._update_expert_hint()

    def _update_expert_visibility(self) -> None:
        expert = bool(settings.get("expert_mode"))
        hybrid = bool(self._iso_hybrid)
        for widget in self._persistence_row_widgets:
            widget.setVisible(expert and self._iso_linux)
        self._persistence_toggle.setEnabled(
            expert
            and self._iso_linux
            and not hybrid
            and not self._wtg_toggle.isChecked()
        )
        self._wtg_toggle.setVisible(expert and self._iso_windows)
        self._wtg_toggle.setEnabled(expert and not hybrid)
        if hybrid:
            self._persistence_toggle.setChecked(False)
            self._wtg_toggle.setChecked(False)
            self._mode_combo.setEnabled(False)
            self._filesystem_combo.setEnabled(False)
            self._partition_combo.setEnabled(False)
            self._target_combo.setEnabled(False)
            index = self._mode_combo.findData("dd")
            if index >= 0:
                self._mode_combo.setCurrentIndex(index)
            self._mode_combo.setToolTip(
                "Hybrid ISO detected \u2014 raw write recommended"
            )
        else:
            self._mode_combo.setEnabled(expert)
            self._filesystem_combo.setEnabled(
                expert and not self._wtg_toggle.isChecked()
            )
            self._partition_combo.setEnabled(expert)
            self._target_combo.setEnabled(expert)
            self._mode_combo.setToolTip("")

    def _set_expert_mode(self, enabled: bool) -> None:
        settings.set_many(expert_mode=bool(enabled))
        self._expert_toggle.setChecked(bool(enabled))
        if hasattr(self, "_settings_expert_toggle"):
            self._settings_expert_toggle.setChecked(bool(enabled))
        self._expert_options_body.setVisible(bool(enabled))
        self._update_expert_visibility()
        self._update_expert_hint()

    def _update_expert_hint(self) -> None:
        if self._iso_hybrid:
            self._raw_hint.setText(
                "Hybrid ISO detected \u2014 the image carries a bootable "
                "MBR, so it is always written raw (DD) to preserve the boot "
                "record."
            )
            return
        mode = self._mode_combo.currentData()
        if mode == "filecopy" and self._wtg_toggle.isChecked():
            self._raw_hint.setText(
                "Windows To Go: the Windows image is applied to the drive "
                "with dism and made bootable (NTFS). The drive is "
                "reformatted."
            )
            return
        if mode == "filecopy" and self._persistence_toggle.isChecked():
            self._raw_hint.setText(
                "Persistence: after copying, a casper-rw persistence image "
                "(or live overlay) is created and the boot config is "
                "patched with the persistent kernel option."
            )
            return
        if mode == "filecopy":
            fs = (self._filesystem_combo.currentData() or "fat32").upper()
            scheme = self._partition_combo.currentData()
            self._raw_hint.setText(
                f"The drive is repartitioned ({scheme}) and the ISO contents "
                f"are copied onto a single {fs} partition (file-copy mode). "
                "Hybrid ISOs are always written raw so their boot record "
                "survives."
            )
        else:
            self._raw_hint.setText(
                "The image is written to the drive as-is (raw image mode)."
            )

    def _build_section_label(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("capLabel")
        return label

    def _help_button(self, tip: str) -> HelpButton:
        bubble = getattr(self, "_help_bubble", None)
        if bubble is None:
            bubble = TipBubble()
            self._help_bubble = bubble
        return HelpButton(tip, bubble)

    def moveEvent(self, event: QMoveEvent | None) -> None:
        assert event is not None
        bubble = getattr(self, "_help_bubble", None)
        if bubble is not None:
            bubble.hide_fast()
        super().moveEvent(event)

    def hideEvent(self, event: QHideEvent | None) -> None:
        assert event is not None
        bubble = getattr(self, "_help_bubble", None)
        if bubble is not None:
            bubble.hide_fast()
        super().hideEvent(event)

    def _build_iso_zone(self) -> QFrame:
        return IsoDropZone()

    def _current_drive_path(self) -> str | None:
        return self._drive_path_for(self._current_drive)

    def _drive_path_for(self, drive: dict[str, Any] | None) -> str | None:
        """Physical-disk path for destructive operations (fail closed).

        Only ``\\\\.\\PHYSICALDRIVEn`` paths are accepted. Volume handles
        (e.g. ``\\\\.\\E:``) would silently write into a single partition
        instead of the whole disk and are never used for flash/wipe/verify.
        """
        if not drive:
            return None
        path: str = drive["physical_path"]
        if path.startswith("\\\\.\\PHYSICALDRIVE"):
            return path
        return None

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
            if busy:
                tip = "Wait for the current operation to finish"
            elif not has_iso:
                tip = "Select an image first"
            elif not has_drive:
                tip = "Select a target drive first"
            else:
                tip = "Write the image to the selected drive"
            self._flash_btn.setToolTip(tip)
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
        if self._backup_worker is not None:
            self._backup_worker.cancel()
        if self._clone_worker is not None:
            self._clone_worker.cancel()

    def _recheck_drive(self, drive: dict[str, Any]) -> dict[str, Any] | None:
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
        if self._sidecar_status in ("mismatch", "error"):
            self._progress.set_error(
                "Image checksum does not match its sidecar \u2014 "
                "flashing blocked"
            )
            return
        if not self._current_drive:
            if self._drives:
                self._show_drive_picker()
                return
            self._progress.set_error(
                "No USB drive detected \u2014 plug one in first"
            )
            return
        drive = self._current_drive
        name = drive["model"] or drive["name"]
        iso_name = os.path.basename(iso)
        expert = bool(settings.get("expert_mode"))
        wtg = bool(
            expert
            and self._iso_windows
            and self._wtg_toggle.isChecked()
        )
        persist = bool(
            expert
            and self._iso_linux
            and self._persistence_toggle.isChecked()
        )
        mode = self._mode_combo.currentData() if expert else "auto"
        if self._iso_hybrid and mode == "filecopy":
            self._progress.set_error(
                "Hybrid ISO detected \u2014 raw write required"
            )
            return
        if (persist or wtg) and mode != "filecopy":
            self._progress.set_error(
                "Persistence / Windows To Go require File copy mode "
                "\u2014 enable it in Expert options"
            )
            return
        persistence_size_mb = 1024
        if persist:
            raw = self._persistence_size.text().strip() or "1024"
            try:
                persistence_size_mb = int(raw)
                if self._persistence_unit.currentData() == "gb":
                    persistence_size_mb *= 1024
            except ValueError:
                self._progress.set_error(
                    "Persistence size must be a whole number of MB/GB"
                )
                return
            if persistence_size_mb <= 0:
                self._progress.set_error(
                    "Persistence size must be greater than zero"
                )
                return
            if persistence_size_mb > 65536:
                self._progress.set_error(
                    "Persistence size is capped at 64 GiB"
                )
                return
        filesystem = (
            self._filesystem_combo.currentData() if expert else "fat32"
        )
        if not self._iso_hybrid and mode != "dd" and filesystem == "fat32":
            largest = iso_mod.largest_iso_file_size(iso)
            if largest > 0xFFFFFFFF:
                self._progress.set_error(
                    "The image contains a file over 4 GiB \u2014 FAT32 "
                    "cannot store it. Pick NTFS or exFAT in Expert mode, "
                    "or write raw (DD)."
                )
                return
        confirm_text = self._confirm_text(drive, iso)
        if self._iso_hybrid:
            confirm_text += (
                "\n\nHybrid ISO: the image is written raw (DD) so its MBR "
                "boot record is preserved."
            )
        elif wtg:
            confirm_text += (
                "\n\nWindows To Go: the drive becomes a portable Windows "
                "workspace (NTFS, bootable via dism/bcdboot)."
            )
        elif persist:
            confirm_text += (
                "\n\nPersistence: a casper-rw / live persistence store is "
                "created on the drive."
            )
        letters = drive.get("letters") or (
            [drive["letter"]] if drive.get("letter") else []
        )
        letter_label = (
            ", ".join(f"{l}:" for l in letters)
            if letters
            else "no drive letter"
        )
        if not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 erase drive and write?",
            message=(
                f"Erase {name} ({letter_label}) and write {iso_name}?"
                f"\n\n{confirm_text}"
            ),
            accept="Erase & write",
            accept_style="danger",
        ):
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

        drive_letters = current.get("letters") or (
            [current["letter"]] if current.get("letter") else []
        )
        writer_kwargs: dict[str, Any] = {
            "verify_after_write": self._verify_toggle.isChecked(),
            "verify_sha256": self._verify_sha_toggle.isChecked(),
            "bad_block_scan": self._bad_block_toggle.isChecked(),
            "bad_block_retries": self._bad_block_retries_value(),
        }
        if expert:
            writer_kwargs.update(
                {
                    "partition_scheme": self._partition_combo.currentData(),
                    "target_system": self._target_combo.currentData(),
                    "filesystem": self._filesystem_combo.currentData(),
                    "write_mode": mode,
                    "persistence": persist,
                    "persistence_size_mb": persistence_size_mb,
                    "windows_to_go": wtg,
                    "chunk_size": self._buffer_combo.currentData()
                    * 1024
                    * 1024,
                    "use_native": self._native_toggle.isChecked(),
                }
            )
        self._begin_write(
            iso, drive_path, drive_letters, writer_kwargs, current
        )

    def _begin_write(
        self,
        iso: str,
        drive_path: str,
        drive_letters: list[str],
        writer_kwargs: dict[str, Any],
        drive: dict[str, Any],
    ) -> None:
        """Reset the UI state and start a UsbWriter with the given options.

        Also used to retry a flash after a failed verification.
        """
        self._progress.reset()
        self._done_bar.setVisible(False)
        self._set_controls_enabled(False)
        self._cancel_btn.setEnabled(True)
        self._wipe_btn.setEnabled(False)
        self._writing = True
        self._write_started = time.perf_counter()
        self._write_duration = 0.0
        self._poller.suspend()
        self._verify_handled = False
        self._last_verify_message = ""
        self._last_verify_digest = ""
        self._verification_in_writer = bool(
            writer_kwargs.get("verify_after_write", False)
        )
        self._retry_payload = (
            iso,
            drive_path,
            drive_letters,
            writer_kwargs,
            drive,
        )
        if self._tray is not None:
            self._tray.setToolTip("Flint \u2014 Writing\u2026")

        writer = UsbWriter(
            iso,
            drive_path,
            letters=drive_letters,
            **writer_kwargs,
        )
        self._writer = writer
        self._write_note: str | None = None
        writer.mode.connect(self._on_write_mode)
        writer.note.connect(self._on_write_note)
        writer.progress.connect(self._on_write_progress)
        writer.speed_mbps.connect(self._progress.set_speed)
        writer.written_bytes.connect(self._progress.set_written)
        writer.total_bytes.connect(self._progress.set_total)
        writer.eta_seconds.connect(self._progress.set_eta)
        writer.phase.connect(self._on_write_phase)
        writer.verify_result.connect(
            lambda ok, msg, res, w=writer: self._on_verify_result(
                ok, msg, res, w
            )
        )
        writer.finished.connect(
            lambda ok, msg, w=writer: self._on_write_finished(ok, msg, w)
        )
        writer.start()

    def _on_write_progress(self, percent: float) -> None:
        self._progress.set_progress(percent)
        self._set_taskbar_progress(percent)
        if self._tray is not None:
            self._tray.setToolTip(f"Flint \u2014 {percent:.0f}%")

    def _on_write_mode(self, mode: str) -> None:
        self._write_was_filecopy = mode == "filecopy"

    def _on_write_note(self, note: str) -> None:
        self._write_note = note

    def _on_write_phase(self, phase: str) -> None:
        if phase == "Verifying":
            # The in-writer verify reuses the write progress bar: freeze the
            # measured write time now (the write itself is done) and switch
            # the chip into verify presentation so stale write speed stats
            # are not shown as if the drive were still being written.
            if self._write_duration == 0.0 and self._write_started > 0:
                self._write_duration = (
                    time.perf_counter() - self._write_started
                )
            self._progress.set_verifying()
            return
        self._progress.set_phase(phase)

    def _on_write_finished(
        self, ok: bool, message: str, worker: UsbWriter
    ) -> None:
        if worker is not self._writer:
            # Stale signal from a superseded writer (e.g. a retry started
            # after this writer finished): never treat it as current, but
            # still collect it for graceful shutdown.
            self._retire(worker)
            return
        if self._write_duration == 0.0:
            self._write_duration = (
                time.perf_counter() - self._write_started
            )
        self._writer = None
        self._retire(worker)
        if (
            not ok
            and message == "cancelled"
            and self._verification_in_writer
            and self._verify_handled
        ):
            # The write itself completed; only the verification was
            # cancelled. Present that honestly instead of a false success.
            self._progress.set_warning(
                "Write completed \u2014 verification was cancelled; "
                "the drive is written but was not verified."
            )
            self._finish_flash(True, "", None)
            return
        if not ok and self._verification_in_writer and self._verify_handled:
            # The writer's own verification already presented its outcome
            # (mismatch/retry dialog); this finished signal is the tail of
            # that flow, not an independent write failure. Never report it
            # a second time.
            return
        if not ok:
            self._finish_flash(False, message or "Write failed", None)
            return
        if self._write_was_filecopy:
            self._finish_flash(
                True,
                "",
                None,
                skipped_verify=True,
                skipped_note=(
                    "Verification skipped \u2014 file-copy writes create a "
                    "filesystem layout, so the drive is not byte-identical "
                    "to the image."
                ),
            )
            if self._write_note:
                self._progress.set_warning(self._write_note)
            return
        if self._verification_in_writer:
            # The writer ran verify_device itself; its outcome arrives via
            # verify_result (before this signal) and may have already
            # handled the flow (retry / abort dialog).
            if self._verify_handled:
                return
            if self._last_verify_message:
                self._progress.set_warning(self._last_verify_message)
            self._finish_flash(True, "", self._last_verify_digest or None)
            return
        if self._verify_toggle.isChecked():
            if self._iso_zone.digest:
                self._start_verify()
                return
            self._finish_flash(True, "", None, skipped_verify=True)
            return
        self._finish_flash(True, "", None)

    def _on_verify_result(
        self,
        ok: bool,
        message: str,
        result: dict[str, Any],
        worker: UsbWriter,
    ) -> None:
        if worker is not self._writer:
            # Stale signal from a superseded writer (a retry or a new flash
            # already replaced it): never block on a dead worker's dialog.
            self._retire(worker)
            return
        if ok:
            self._last_verify_message = message
            self._last_verify_digest = result.get("digest", "")
            return
        if message == "cancelled":
            self._verify_handled = True
            self._last_verify_message = (
                "Write completed \u2014 verification was cancelled"
            )
            return
        # Mismatches or unreadable sectors: offer to retry the write.
        self._verify_handled = True
        mismatches = len(result.get("mismatches", []))
        bad = len(result.get("bad_sectors", []))
        if dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 verification failed",
            message=(
                "The write-back check found problems with the drive.\n\n"
                f"{mismatches} mismatched region(s) and {bad} unreadable "
                "sector(s). The drive may be damaged or the image was not "
                "written correctly.\n\nRetry the write, or abort?"
            ),
            accept="Retry write",
        ):
            if self._retry_payload is not None:
                self._begin_write(*self._retry_payload)
        else:
            self._finish_flash(False, message or "Verification failed", None)

    def _retire(self, worker: QThread) -> None:
        if worker is None:
            return
        self._retired_workers.append(worker)
        # Keep finished worker objects around only for graceful shutdown;
        # drop the oldest so long sessions do not accumulate them forever.
        # Never deleteLater a thread that is still running: that destroys
        # the QThread while its run() executes and aborts the process.
        if len(self._retired_workers) > 16:
            oldest = self._retired_workers.pop(0)
            if oldest is not None and not oldest.isRunning():
                oldest.deleteLater()

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
        verifier.finished.connect(
            lambda ok, msg, v=verifier: self._on_verify_finished(ok, msg, v)
        )
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

    def _on_verify_finished(
        self, ok: bool, message: str, worker: VerifyWorker
    ) -> None:
        if worker is not self._verifier:
            # Stale signal from a superseded verifier: ignore it, but still
            # collect the (finished) thread object.
            self._retire(worker)
            return
        self._verifier = None
        self._retire(worker)
        if message == "cancelled":
            # The write (if any) already completed; this only cancels the
            # read-back. Do not claim the drive was left partially written.
            self._progress.set_warning(
                "Write completed \u2014 verification was cancelled; "
                "the drive is written but was not verified."
            )
            self._finish_flash(True, "", None)
            return
        if ok:
            self._finish_flash(True, "", message)
        else:
            self._finish_flash(False, message or "Verification failed", None)

    def _on_wipe_clicked(self, method: str = "zero") -> None:
        if self._busy():
            return
        if not self._current_drive:
            self._progress.set_error("Select a USB drive first")
            return
        drive = self._current_drive
        name = drive.get("model") or drive.get("name")
        letter = drive.get("letter") or "no drive letter"
        method_text = {
            "zero": "replace every byte with zeros",
            "nist": "replace every byte with random data",
            "dod": "overwrite every byte three times (zeros, ones, "
            "random \u2014 DoD 5220.22-M)",
        }
        if not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 erase drive?",
            message=(
                f"Erase {name} ({letter}) \u2014 "
                f"{method_text.get(method, method_text['zero'])}?"
                f"\n\n{self._confirm_text(drive)}"
            ),
            accept="Erase & wipe",
            accept_style="danger",
        ):
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
        self._poller.suspend()
        if self._tray is not None:
            self._tray.setToolTip("Flint \u2014 Wiping\u2026")

        worker = WipeWorker(drive_path, letters, method=method)
        self._wipe_worker = worker
        self._wipe_method = method
        self._wipe_verify = None
        worker.progress.connect(self._on_write_progress)
        worker.speed_mbps.connect(self._progress.set_speed)
        worker.written_bytes.connect(self._progress.set_written)
        worker.total_bytes.connect(self._progress.set_total)
        worker.eta_seconds.connect(self._progress.set_eta)
        worker.phase.connect(self._progress.set_phase)
        worker.verified.connect(self._on_wipe_verified)
        worker.finished.connect(self._on_wipe_finished)
        worker.start()

    def _on_wipe_verified(self, ok: bool, message: str) -> None:
        self._wipe_verify = (ok, message)

    def _format_wipe_verify(self) -> str:
        if self._wipe_verify is None:
            return "not run"
        ok, message = self._wipe_verify
        return f"{'verified' if ok else 'NOT verified'} ({message})"

    def _on_wipe_finished(self, ok: bool, message: str) -> None:
        worker = self._wipe_worker
        self._wipe_worker = None
        if worker is not None:
            self._retire(worker)
        self._writing = False
        self._poller.resume()
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
        report: dict[str, Any] | None = None
        if target is not None:
            report = flash_report(
                "\u2014 wipe \u2014",
                target["model"] or target["name"],
                time.perf_counter() - self._write_started,
                verified=False,
                success=ok,
                drive_serial=target.get("serial"),
                wipe_verified=self._format_wipe_verify(),
            )
            append_history(report)
        # Copy-report must offer the wipe report (not a previous flash's).
        self._last_report = report
        if self._tray is not None:
            self._tray.setToolTip(
                "Flint \u2014 Wipe done" if ok else "Flint"
            )
        if ok:
            wipe_done = {
                "zero": "every byte was replaced with zeros.",
                "nist": "every byte was replaced with random data.",
                "dod": "every byte was overwritten three times (zeros, "
                "ones, random).",
            }
            method = getattr(self, "_wipe_method", "zero")
            dialogs.completion(
                self,
                kind="success",
                title="Drive wiped",
                message=(
                    f"{target.get('model') or 'drive'} was erased: "
                    f"{wipe_done.get(method, wipe_done['zero'])}"
                    if target is not None
                    else f"The drive was erased: "
                    f"{wipe_done.get(method, wipe_done['zero'])}"
                ),
                buttons=[("Close", "primary", "close")],
            )
        elif message == "cancelled":
            dialogs.completion(
                self,
                kind="warning",
                title="Wipe cancelled",
                message=(
                    "The drive was partially wiped. Run wipe again to "
                    "finish erasing it."
                ),
                buttons=[("Close", "primary", "close")],
            )
        else:
            dialogs.completion(
                self,
                kind="error",
                title="Wipe failed",
                message=self._friendly_error(message or "Wipe failed"),
                buttons=[("Close", "primary", "close")],
            )
        self._active_write_drive = None
        self._update_controls_state()

    def _queue_images(self) -> list[str]:
        items: list[str] = []
        for i in range(self._queue_list.count()):
            item = self._queue_list.item(i)
            if item is not None:
                items.append(item.text())
        return items

    def _on_queue_add_clicked(self) -> None:
        if self._busy():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add images to the queue",
            "",
            "Disk images (*.iso *.img);;All files (*)",
        )
        existing = set(self._queue_images())
        for path in paths:
            if path not in existing:
                self._queue_list.addItem(path)
                existing.add(path)

    def _on_queue_remove_clicked(self) -> None:
        if self._busy():
            return
        for item in self._queue_list.selectedItems():
            self._queue_list.takeItem(self._queue_list.row(item))

    def _on_queue_clear_clicked(self) -> None:
        if self._busy():
            return
        self._queue_list.clear()

    def _on_check_updates_clicked(self) -> None:
        if self._update_checker is not None:
            return
        worker = UpdateCheckWorker()
        self._update_checker = worker
        worker.finished_check.connect(self._on_update_check_done)
        worker.start()

    def _on_update_check_done(
        self, ok: bool, message: str, release: object
    ) -> None:
        self._update_checker = None
        if not ok:
            dialogs.inform(
                self,
                kind="warning",
                title="Update check failed",
                message=message or "Could not check for updates.",
            )
            return
        data = release if isinstance(release, dict) else {}
        tag = str(data.get("tag_name") or "")
        latest = version_from_tag(tag)
        if compare_version(APP_VERSION, latest) != -1:
            dialogs.inform(
                self,
                kind="success",
                title="Flint is up to date",
                message=(
                    f"You're running the latest version ({APP_VERSION})."
                ),
            )
            return
        asset = release_executable(data)
        if asset is None:
            dialogs.inform(
                self,
                kind="warning",
                title="Update available",
                message=(
                    f"Flint {latest} is available (you have {APP_VERSION}), "
                    "but no flint.exe asset was found on the release page."
                ),
            )
            return
        downloadable = asset.get("browser_download_url") or ""
        result = dialogs.completion(
            self,
            kind="warning",
            title="Update available",
            message=(
                f"Flint {latest} is available (you have {APP_VERSION}).\n\n"
                "The new version will be downloaded and its SHA-256 "
                f"verified before you can run it."
            ),
            buttons=[
                ("Download", "primary", "download"),
                ("Later", "ghost", "close"),
            ],
        )
        if result == "download" and downloadable:
            self._start_update_download(
                downloadable, default_download_path(latest), data
            )

    def _start_update_download(
        self, url: str, dest: str, release: dict[str, Any]
    ) -> None:
        if self._update_downloader is not None:
            return
        digest = fetch_sidecar_digest(sidecar_digest_url(release))
        worker = UpdateDownloadWorker(url, dest, digest)
        self._update_downloader = worker
        self._pending_update_path = dest
        worker.progress.connect(self._on_update_download_progress)
        worker.finished_download.connect(self._on_update_download_done)
        worker.start()
        self._progress.reset()
        self._progress.set_phase("Downloading update\u2026")

    def _on_update_download_progress(self, done: int, total: int) -> None:
        pct = done / total * 100.0 if total > 0 else 0.0
        self._progress.set_progress(pct)
        self._set_taskbar_progress(pct)

    def _on_update_download_done(self, ok: bool, result: str) -> None:
        self._update_downloader = None
        self._set_taskbar_progress(None)
        if not ok:
            self._progress.set_error(
                self._friendly_error(result or "Update download failed")
            )
            dialogs.completion(
                self,
                kind="error",
                title="Update download failed",
                message=result or "The update could not be downloaded.",
                buttons=[("Close", "primary", "close")],
            )
            return
        self._progress.set_done()
        self._progress._title.setText("Update ready")
        choice = dialogs.completion(
            self,
            kind="success",
            title="Update downloaded",
            message=(
                "The new Flint was downloaded and its SHA-256 verified.\n\n"
                "Close Flint and run the new executable to update."
            ),
            buttons=[
                ("Show file", "primary", "reveal"),
                ("Close", "ghost", "close"),
            ],
        )
        if choice == "reveal":
            parent = Path(self._pending_update_path).parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(parent)))

    def _maybe_auto_check_updates(self) -> None:
        """Quiet 7-day update check: never blocks, only surfaces an update."""
        from core.updates import should_auto_check

        if self._update_checker is not None or self._busy():
            return
        last = settings.get("last_update_check")
        try:
            last_f = float(last) if last is not None else None
        except (TypeError, ValueError):
            last_f = None
        if not should_auto_check(last_f):
            return
        settings.set_many(last_update_check=time.time())
        worker = UpdateCheckWorker()
        self._update_checker = worker
        worker.finished_check.connect(self._on_auto_update_check_done)
        worker.start()

    def _on_auto_update_check_done(
        self, ok: bool, message: str, release: object
    ) -> None:
        self._update_checker = None
        if not ok:
            return
        data = release if isinstance(release, dict) else {}
        tag = str(data.get("tag_name") or "")
        if compare_version(APP_VERSION, version_from_tag(tag)) == -1:
            self._on_update_check_done(True, "", release)

    def _mark_queue_item(self, index: int, state: str) -> None:
        item = self._queue_list.item(index)
        if item is not None:
            sep = " \u00b7 "
            item.setText(f"{state}{sep}{item.text().rsplit(sep, 1)[-1]}")

    def _on_flash_queue_clicked(self) -> None:
        if self._busy():
            return
        images = self._queue_images()
        if not images:
            self._progress.set_error("Add images to the queue first")
            return
        if not self._current_drive:
            self._progress.set_error("Select a USB drive first")
            return
        drive = self._current_drive
        name = drive.get("model") or drive.get("name")
        if not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 flash queue?",
            message=(
                f"Write {len(images)} image"
                f"{'s' if len(images) != 1 else ''} to {name}?\n\n"
                f"Every image replaces the whole drive; all existing data "
                f"is erased.\n\n{self._confirm_text(drive)}"
            ),
            accept=f"Flash {len(images)}",
            accept_style="danger",
        ):
            return
        current = self._recheck_drive(drive)
        if current is None:
            self._progress.set_error(
                "Drive changed or disconnected \u2014 refresh and re-pick"
            )
            return
        if not self._require_typed_confirmation(current, None):
            self._progress.set_error("Confirmation failed — aborting")
            return
        self._queue_items = images
        self._queue_index = 0
        self._queue_ok = 0
        self._queue_active = True
        for index in range(self._queue_list.count()):
            self._mark_queue_item(index, "pending")
        self._start_queue_item(0)

    def _start_queue_item(
        self, index: int, drive: dict[str, Any] | None = None
    ) -> None:
        images = (
            self._fleet.images
            if self._fleet is not None and self._fleet_busy
            else self._queue_items
        )
        if index >= len(images):
            return
        image = images[index]
        selected: dict[str, Any] | None = (
            drive if drive is not None else self._current_drive
        )
        if selected is None:
            if self._fleet_busy:
                self._disarm_fleet("no drive available")
            else:
                self._fail_queue("no drive selected")
            return
        if not (self._fleet is not None and self._fleet_busy):
            self._mark_queue_item(index, "flashing")
        # Per-item safety: an unreadable or mismatching sidecar blocks the
        # item cheaply (digest unknown here; the writer hashes the image
        # itself during verification).
        status, _detail = checksum_mod.check_sidecar(image, None)
        if status in ("error", "mismatch"):
            if self._fleet_busy:
                self._disarm_fleet(f"checksum problem on {image}")
            else:
                self._fail_queue(f"checksum problem on {image}")
            return
        letters = selected.get("letters") or (
            [selected["letter"]] if selected.get("letter") else []
        )
        expert = bool(settings.get("expert_mode"))
        writer_kwargs = {
            "verify_after_write": self._verify_toggle.isChecked(),
            "verify_sha256": self._verify_sha_toggle.isChecked(),
            "bad_block_scan": self._bad_block_toggle.isChecked(),
            "bad_block_retries": self._bad_block_retries_value(),
        }
        if expert:
            writer_kwargs.update(
                {
                    "partition_scheme": self._partition_combo.currentData(),
                    "target_system": self._target_combo.currentData(),
                    "filesystem": self._filesystem_combo.currentData(),
                    "write_mode": self._mode_combo.currentData(),
                    "chunk_size": self._buffer_combo.currentData()
                    * 1024
                    * 1024,
                    "use_native": self._native_toggle.isChecked(),
                }
            )
        self._begin_write(
            image,
            self._drive_path_for(selected) or "",
            letters,
            writer_kwargs,
            selected,
        )

    def _fail_queue(self, reason: str) -> None:
        self._queue_active = False
        self._writing = False
        self._poller.resume()
        self._set_controls_enabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.set_error(self._friendly_error(reason or "Queue failed"))
        dialogs.completion(
            self,
            kind="error",
            title="Queue failed",
            message=(
                f"The queue stopped at image {self._queue_index + 1} of "
                f"{len(self._queue_items)}: {reason}"
            ),
            buttons=[("Close", "primary", "close")],
        )

    def _maybe_start_next_queue_item(self) -> None:
        """Called after each flash finishes while a queue is active."""
        if not self._queue_active:
            return
        index = self._queue_index
        succeeded = self._queue_last_succeeded
        self._queue_index += 1
        if succeeded:
            self._queue_ok += 1
            self._mark_queue_item(index, "done")
            if self._queue_index < len(self._queue_items):
                self._start_queue_item(self._queue_index)
            else:
                self._queue_active = False
                dialogs.completion(
                    self,
                    kind="success",
                    title="Queue complete",
                    message=(
                        f"All {len(self._queue_items)} image"
                        f"{'s' if len(self._queue_items) != 1 else ''} "
                        "were written to "
                        f"{(self._active_write_drive or self._current_drive or {}).get('model') or 'the drive'}."
                    ),
                    buttons=[("Close", "primary", "close")],
                )
        else:
            self._mark_queue_item(index, "failed")
            self._queue_active = False
            self._progress.set_error(
                self._friendly_error(
                    "The queue stopped at image "
                    f"{index + 1} of {len(self._queue_items)}"
                )
            )
            dialogs.completion(
                self,
                kind="error",
                title="Queue stopped",
                message=(
                    f"Image {index + 1} of {len(self._queue_items)} failed. "
                    "The remaining images were not flashed."
                ),
                buttons=[("Close", "primary", "close")],
            )

    def _on_fleet_toggled(self, checked: bool) -> None:
        if checked:
            self._arm_fleet()
        else:
            self._disarm_fleet()

    def _on_fleet_stop_clicked(self) -> None:
        if self._fleet_busy:
            self._on_cancel_clicked()
        else:
            self._disarm_fleet("Fleet mode stopped")

    def _arm_fleet(self) -> None:
        if self._fleet is not None:
            return
        images = self._queue_images()
        if not images:
            self._fleet_toggle.setChecked(False)
            dialogs.inform(
                self,
                kind="warning",
                title="Fleet mode",
                message="Add images to the queue first.",
            )
            return
        if self._busy():
            self._fleet_toggle.setChecked(False)
            return
        typed, accepted = dialogs.input_text(
            self,
            title="Arm fleet mode?",
            message=(
                "Fleet mode writes every image in the queue to EVERY "
                "removable drive you plug in, automatically, erasing all "
                "existing data on each one.\n\n"
                f"{len(images)} image"
                f"{'s' if len(images) != 1 else ''} queued.\n\n"
                "Type ARM to arm fleet mode."
            ),
            placeholder="ARM",
        )
        if not accepted or typed.strip().upper() != "ARM":
            self._fleet_toggle.setChecked(False)
            return
        self._fleet = FleetSession(images=images)
        self._fleet_busy = False
        self._fleet_image_index = 0
        self._fleet_drive = None
        self._fleet_banner.setVisible(True)
        self._fleet_update_banner(
            "Armed \u2014 waiting for a drive that fits the queue\u2026"
        )
        self._fleet_tick()

    def _disarm_fleet(self, reason: str | None = None) -> None:
        if self._fleet is None and not self._fleet_busy:
            return
        was_busy = self._fleet_busy
        self._fleet = None
        self._fleet_busy = False
        self._fleet_image_index = 0
        self._fleet_drive = None
        self._fleet_banner.setVisible(False)
        if self._fleet_toggle.isChecked():
            self._fleet_toggle.setChecked(False)
        if reason and not was_busy:
            self._progress.set_error(reason)

    def _fleet_tick(self) -> None:
        session = self._fleet
        if session is None:
            return
        if self._fleet_busy or self._writing:
            return
        if session.expired():
            self._disarm_fleet()
            dialogs.inform(
                self,
                kind="info",
                title="Fleet mode",
                message=(
                    "Fleet mode expired after an hour without activity. "
                    "Re-arm to continue flashing drives."
                ),
            )
            return
        drive = fleet.pick_candidate(
            self._drives, session,
            skip_flashed=self._fleet_skip_flashed.isChecked(),
        )
        if drive is not None:
            self._fleet_start_drive(drive)

    def _fleet_start_drive(self, drive: dict[str, Any]) -> None:
        self._fleet_drive = drive
        self._fleet_image_index = 0
        self._fleet_busy = True
        name = drive.get("model") or drive.get("name") or "the drive"
        self._fleet_update_banner(f"Writing to {name}\u2026")
        self._start_queue_item(0, drive)

    def _fleet_finish_image(self, succeeded: bool) -> None:
        session = self._fleet
        if session is None or not self._fleet_busy:
            return
        self._fleet_busy = False
        drive = self._fleet_drive
        if not succeeded:
            self._fleet_drive = None
            self._disarm_fleet()
            dialogs.completion(
                self,
                kind="error",
                title="Fleet stopped",
                message=(
                    "A flash failed, so fleet mode stopped. Fix the problem "
                    "and re-arm to continue with the remaining drives."
                ),
                buttons=[("Close", "primary", "close")],
            )
            return
        if drive is None:
            self._disarm_fleet()
            return
        self._fleet_image_index += 1
        if self._fleet_image_index < len(session.images):
            self._fleet_busy = True
            name = drive.get("model") or drive.get("name") or "the drive"
            self._fleet_update_banner(f"Writing to {name}\u2026")
            self._start_queue_item(self._fleet_image_index, drive)
            return
        session.mark_flashed(drive)
        self._fleet_drive = None
        self._fleet_image_index = 0
        self._fleet_update_banner(
            f"{session.done_count} done \u2014 waiting for the next drive\u2026"
        )
        self._fleet_tick()

    def _fleet_update_banner(self, text: str) -> None:
        self._fleet_label.setText(text)

    def _start_drive_operation(self, worker: Any) -> None:
        """Common busy-state setup for backup/clone operations."""
        self._progress.reset()
        self._done_bar.setVisible(False)
        self._set_controls_enabled(False)
        self._cancel_btn.setEnabled(True)
        self._flash_btn.setEnabled(False)
        self._wipe_btn.setEnabled(False)
        self._writing = True
        self._write_started = time.perf_counter()
        self._write_duration = 0.0
        self._poller.suspend()
        worker.progress.connect(self._on_write_progress)
        worker.speed_mbps.connect(self._progress.set_speed)
        worker.written_bytes.connect(self._progress.set_written)
        worker.total_bytes.connect(self._progress.set_total)
        worker.eta_seconds.connect(self._progress.set_eta)
        worker.phase.connect(self._on_drive_phase)
        worker.finished.connect(self._on_drive_operation_finished)
        worker.start()

    def _on_drive_phase(self, phase: str) -> None:
        self._progress.set_phase(phase)
        self._progress._title.setText(phase)

    def _on_backup_clicked(self) -> None:
        if self._busy():
            return
        drive = self._current_drive
        if drive is None:
            self._progress.set_error("Select a USB drive first")
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
        name = current.get("model") or current.get("name")
        size = DriveDetector.format_size(
            current["size_gb"] * 1_000_000_000
        )
        if not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 back up drive?",
            message=(
                f"Read {name} ({size}) into an image file.\n\n"
                "The drive is locked and unmounted while reading; nothing "
                "on it is changed."
            ),
            accept="Back up",
            accept_style="primary",
        ):
            return
        default_name = f"flint-backup-{time.strftime('%Y%m%d-%H%M%S')}.img"
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save backup image", default_name, "Disk image (*.img)"
        )
        if not out_path:
            return
        self._backup_digest = ""
        worker = BackupWorker(
            drive_path,
            out_path,
            letters=current.get("letters")
            or ([current["letter"]] if current.get("letter") else []),
        )
        worker.digest.connect(lambda d: setattr(self, "_backup_digest", d))
        self._backup_worker = worker
        self._backup_out = out_path
        self._backup_drive = current
        self._start_drive_operation(worker)

    def _on_clone_clicked(self) -> None:
        if self._busy():
            return
        source = self._current_drive
        if source is None:
            self._progress.set_error("Select a USB drive first")
            return
        current_source = self._recheck_drive(source)
        if current_source is None:
            self._progress.set_error(
                "Drive changed or disconnected \u2014 refresh and re-pick"
            )
            return
        menu = QMenu(self)
        label = "Clone onto\u2026"
        action = menu.addAction(label)
        assert action is not None
        action.setEnabled(False)
        menu.addSeparator()
        for drive in self._drives:
            if drive.get("physical_path") == current_source.get(
                "physical_path"
            ):
                continue
            size = DriveDetector.format_size(
                drive["size_gb"] * 1_000_000_000
            )
            serial = self._serial_tail(drive)
            letters = drive.get("letters") or (
                [drive["letter"]] if drive.get("letter") else []
            )
            letter_label = (
                ", ".join(f"{l}:" for l in letters)
                if letters
                else "no drive letter"
            )
            text = f"{drive['model'] or drive['name']} \u00b7 {size} ({letter_label})"
            act = menu.addAction(text)
            assert act is not None
            if serial:
                act.setToolTip(f"S/N {serial}")
            act.setData(drive)
        target_action = menu.exec(QCursor.pos())
        if target_action is None:
            return
        target = target_action.data()
        if target is None:
            self._progress.set_error("No target drive selected")
            return
        if (target["size_gb"] * 1_000_000_000) < (
            current_source["size_gb"] * 1_000_000_000
        ):
            self._progress.set_error(
                "Target drive is smaller than the source \u2014 clone refused"
            )
            return
        src_name = current_source.get("model") or current_source.get("name")
        dst_name = target.get("model") or target.get("name")
        if not dialogs.confirm(
            self,
            kind="warning",
            title="Flint \u2014 clone drive?",
            message=(
                f"Copy {src_name} onto {dst_name}.\n\n"
                f"{dst_name} is erased completely \u2014 every byte is "
                f"replaced with the source contents.\n\n"
                f"{self._confirm_text(target)}"
            ),
            accept="Erase & clone",
            accept_style="danger",
        ):
            return
        # The target is re-checked against the live list before anything
        # destructive happens.
        fresh_target = self._recheck_drive(target)
        if fresh_target is None:
            self._progress.set_error(
                "Target drive changed or disconnected \u2014 refresh and "
                "re-pick"
            )
            return
        if not self._require_typed_confirmation(fresh_target, None):
            self._progress.set_error("Confirmation failed — aborting")
            return
        worker = CloneWorker(
            current_source["physical_path"],
            fresh_target["physical_path"],
            source_letters=current_source.get("letters") or [],
            target_letters=fresh_target.get("letters") or [],
        )
        self._clone_worker = worker
        self._clone_source = current_source
        self._clone_target = fresh_target
        self._start_drive_operation(worker)

    def _on_drive_operation_finished(self, ok: bool, message: str) -> None:
        worker = self._backup_worker or self._clone_worker
        self._backup_worker = None
        self._clone_worker = None
        if worker is not None:
            self._retire(worker)
        self._writing = False
        self._poller.resume()
        self._set_controls_enabled(True)
        self._cancel_btn.setEnabled(False)
        self._set_taskbar_progress(None, error=not ok)
        is_backup = bool(self._backup_out)
        if ok:
            self._progress.set_done()
            self._progress._title.setText("Backed up" if is_backup else "Cloned")
            if is_backup:
                digest = self._backup_digest
                self._done_label.setText("Backup complete")
                ellipsis_ = "\u2026"
                self._done_summary.setText(
                    f"{self._backup_out} \u00b7 SHA256 "
                    f"{digest[:12] + ellipsis_ if digest else 'n/a'}"
                )
            else:
                src = self._clone_source or {}
                dst = self._clone_target or {}
                self._done_label.setText("Clone complete")
                self._done_summary.setText(
                    f"{src.get('model') or 'source'} \u2192 "
                    f"{dst.get('model') or 'target'}"
                )
            self._done_bar.setVisible(True)
        else:
            self._progress.set_error(
                self._friendly_error(message or "Operation failed")
            )
            if self._tray is not None:
                self._tray.showMessage(
                    "Flint \u2014 operation finished",
                    "Backup/clone failed." if not is_backup else "Backup failed.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    4000,
                )
        self._backup_out = ""
        self._update_controls_state()
        if not ok:
            dialogs.completion(
                self,
                kind="warning" if message == "cancelled" else "error",
                title=(
                    "Backup cancelled"
                    if message == "cancelled" and is_backup
                    else "Clone cancelled"
                    if message == "cancelled"
                    else "Backup failed"
                    if is_backup
                    else "Clone failed"
                ),
                message=self._friendly_error(message or "Operation failed"),
                buttons=[("Close", "primary", "close")],
            )

    def _on_eject_clicked(self) -> None:
        if self._ejecting:
            return
        target = self._active_write_drive or self._current_drive
        fresh = None
        if target is not None:
            try:
                # Re-resolve the drive: the completion popup may stay open
                # for minutes, during which the drive could have been
                # swapped. Ejecting a different physical drive would be a
                # surprise, so only eject the serial that was written.
                fresh = self._recheck_drive(target)
            except Exception:
                logger.exception("drive recheck failed during eject")
            if fresh is None:
                dialogs.inform(
                    self,
                    kind="warning",
                    title="Eject",
                    message=(
                        "The drive is no longer present \u2014 it may "
                        "already have been removed."
                    ),
                )
                return
            path = self._drive_path_for(fresh)
        else:
            path = None
        if not path:
            dialogs.inform(
                self,
                kind="info",
                title="Eject",
                message="No drive selected.",
            )
            return
        self._ejecting = True
        try:
            try:
                ok, msg = eject_drive(path)
            except Exception as exc:
                ok, msg = False, str(exc) or "eject failed"
            if ok:
                dialogs.inform(
                    self,
                    kind="success",
                    title="Eject",
                    message="Drive ejected \u2014 safe to unplug.",
                )
            else:
                dialogs.inform(
                    self,
                    kind="error",
                    title="Eject",
                    message=msg or "Windows refused to eject the drive.",
                )
        finally:
            self._ejecting = False

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
        if "access denied" in lowered:
            return (
                f"{message} \u2014 run Flint as administrator, or close any "
                "program using the drive."
            )
        if "device not ready" in lowered or "could not determine device size" in lowered:
            return (
                f"{message} \u2014 the drive may not be responding. Try a "
                "different USB port or cable."
            )
        if "verification failed" in lowered:
            return (
                f"{message} \u2014 re-download the ISO; the source image "
                "may be corrupt."
            )
        if "volume" in lowered and "in use" in lowered:
            return (
                f"{message} \u2014 close Explorer windows, antivirus "
                "real-time scanning, or other tools accessing the drive."
            )
        if "cancelled" in lowered:
            return (
                f"{message} The drive may be partially written \u2014 "
                "flash again before use."
            )
        return message

    def _on_copy_report_clicked(self) -> None:
        if self._last_report is None:
            return
        text = self._build_report_text(self._last_report)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        if self._tray is not None:
            self._tray.showMessage(
                "Flint",
                "Flash report copied to clipboard.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    @staticmethod
    def _build_report_text(entry: dict[str, Any]) -> str:
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
            f"Wipe verified: {entry.get('wipe_verified') or 'not run'}",
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
        skipped_note: str | None = None,
    ) -> None:
        self._writing = False
        self._poller.resume()
        self._verify_hint.setVisible(False)
        self._set_controls_enabled(True)
        self._cancel_btn.setEnabled(False)
        self._wipe_btn.setEnabled(self._current_drive is not None)
        self._set_taskbar_progress(None, error=not succeeded)
        self._writer = None
        self._verifier = None
        self._update_controls_state()

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
            except Exception:
                logger.exception("probe_bootability failed")
                boot = "unknown"

        if succeeded:
            self._progress.set_done()
            if skipped_verify:
                self._progress.set_warning(
                    skipped_note
                    or (
                        "Verification skipped \u2014 the image digest is "
                        "unavailable. Re-select the image and flash again to "
                        "verify."
                    )
                )
                self._done_label.setText("Flash complete \u2014 not verified")
            else:
                self._done_label.setText(
                    "Verified \u2713\ufe0e" if verified_sha else "Flash complete"
                )
            self._done_bar.setVisible(True)
        elif error_text == "cancelled":
            self._progress.set_error(
                "Write cancelled \u2014 the drive was left partially "
                "written and is not usable. Flash again before using it."
            )
            self._progress._title.setText("Cancelled")
            self._done_label.setText("Write cancelled")
            self._done_summary.setText(
                "The drive needs a complete write before use."
            )
            self._done_bar.setVisible(True)
        else:
            self._progress.set_error(
                self._friendly_error(error_text or "Failed")
            )
            self._progress._title.setText("Failed")
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

        report: dict[str, Any] | None = None
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

        toast_msg = (
            "Image was " + ("verified." if verified_sha else "written.")
            if succeeded
            else ("Write cancelled" if error_text == "cancelled" else "Write failed")
        )
        self._show_toast("Flint", toast_msg)

        if succeeded:
            if skipped_verify:
                kind = "warning"
                title = "Flash complete \u2014 not verified"
                detail = skipped_note or (
                    "The image was written, but verification was skipped: "
                    "the image digest was unavailable. Re-select the image "
                    "and flash again to verify."
                )
            else:
                kind = "success"
                title = "Flash complete"
                detail = (
                    "The image was written and verified."
                    if verified_sha
                    else "The image was written but verification was "
                    "cancelled."
                )
            if boot and boot != "unknown":
                detail += f"\n\nBoot: {boot}"
        elif error_text == "cancelled":
            kind = "warning"
            title = "Write cancelled"
            detail = (
                "The drive was left partially written and is not usable. "
                "Flash again before using it."
            )
        else:
            kind = "error"
            title = "Flash failed"
            detail = self._friendly_error(error_text or "Failed")

        # Per-item popups are suppressed while a queue or fleet write is
        # running; the queue logic shows a single summary at the end and
        # fleet reports progress in its banner instead.
        if not self._queue_active and not self._fleet_busy:
            if succeeded:
                result = dialogs.completion(
                    self, kind=kind, title=title, message=detail
                )
                if result == "eject":
                    self._on_eject_clicked()
                elif result == "copy":
                    self._on_copy_report_clicked()
            else:
                buttons = (
                    [("Close", "primary", "close")]
                    if error_text == "cancelled"
                    else [
                        ("Copy report", "ghost", "copy"),
                        ("Close", "primary", "close"),
                    ]
                )
                dialogs.completion(
                    self,
                    kind=kind,
                    title=title,
                    message=detail,
                    buttons=buttons,
                )

        if self._queue_active:
            self._queue_last_succeeded = succeeded
            self._maybe_start_next_queue_item()
        if self._fleet_busy:
            self._fleet_finish_image(succeeded)
        elif self._fleet is not None and succeeded and target is not None:
            # A manual flash finished while fleet is armed: stamp the
            # drive so the session does not flash it again.
            self._fleet.mark_flashed(target)