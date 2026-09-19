"""Standalone widget classes extracted from window.py.

These classes are self-contained UI components that do not depend on
MainWindow and can be reused across the application.
"""

import logging
import os
import time
from collections import deque
from collections.abc import Callable

from PyQt6.QtCore import (  # type: ignore[attr-defined]
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QEnterEvent,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import settings
from core.drives import DriveDetector
from ui import style
from ui.chamfer import ChamferPanel

logger = logging.getLogger("flint")


def _windows_uses_dark_mode() -> bool:
    """Return True when Windows is configured for dark app mode."""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return int(value) == 0
    except Exception:
        return True  # default to dark on failure


def _restyle(widget: QWidget) -> None:
    """Repolish a widget so property-driven stylesheet selectors apply."""
    s = widget.style()
    if s is not None:
        s.unpolish(widget)
        s.polish(widget)


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
    "bypass_tpm": (
        "Injects registry keys into boot.wim to skip Windows 11 "
        "TPM 2.0, Secure Boot and RAM checks during setup. "
        "Requires file-copy mode."
    ),
}


class TipBubble(QFrame):
    """Small always-on-top bubble for the (?) help buttons."""

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
        try:
            self._anim.stop()
            self._anim.setStartValue(self.windowOpacity())
            self._anim.setEndValue(goal)
            self._anim.start()
        except RuntimeError:
            return

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
            digest = __import__("hashlib").sha256()
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
    detected = pyqtSignal(str, bool, bool, bool)

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


class DecompressWorker(QThread):
    """Decompress an archive in a background thread."""

    done = pyqtSignal(str, bool, str, str)  # (original_path, ok, extracted_path, tmp_dir)

    def __init__(self, path: str, tmp_dir: str, fmt: str) -> None:
        super().__init__()
        self._path = path
        self._tmp_dir = tmp_dir
        self._fmt = fmt

    def run(self) -> None:
        from core.decompress import (
            _decompress_gz,
            _decompress_xz,
            _decompress_zip,
        )

        try:
            if self._fmt == ".zip":
                extracted = _decompress_zip(self._path, self._tmp_dir)
            elif self._fmt == ".gz":
                extracted = _decompress_gz(self._path, self._tmp_dir)
            elif self._fmt == ".xz":
                extracted = _decompress_xz(self._path, self._tmp_dir)
            else:
                self.done.emit(self._path, False, "", "")
                return
            self.done.emit(self._path, True, extracted, self._tmp_dir)
        except Exception:
            logger.exception("DecompressWorker failed for %s", self._path)
            self.done.emit(self._path, False, "", "")


class IsoDropZone(QFrame):
    iso_selected = pyqtSignal(str)
    iso_analysis = pyqtSignal(str, bool, bool, bool)
    hash_done = pyqtSignal(str, bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("isoDropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._path: str | None = None
        self._decompressed_path: str | None = None
        self._worker: IsoWorker | None = None
        self._analyzer: IsoDetectWorker | None = None
        self._decompress_worker: DecompressWorker | None = None
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
            "Only image files (.iso, .img, .bin) or compressed archives (.zip, .gz, .xz, .zst) are supported"
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

        self._iso_check = QLabel("")
        self._iso_check.setObjectName("isoCheck")
        self._iso_check.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._iso_check.setVisible(False)

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
            self.setProperty("dragging", True)
            _restyle(self)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent | None) -> None:
        assert event is not None
        mime = event.mimeData()
        assert mime is not None
        if mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent | None) -> None:
        self.setProperty("dragging", False)
        _restyle(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent | None) -> None:
        assert event is not None
        self.setProperty("dragging", False)
        _restyle(self)
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
            self._drop_timer.start(3000)
            event.acceptProposedAction()

    def _first_iso_url(self, event: QDropEvent) -> QUrl | None:
        mime = event.mimeData()
        if mime is None:
            return None
        _IMAGE_EXTS = (".iso", ".img", ".bin")
        _COMPRESSED_EXTS = (".zip", ".gz", ".xz", ".zst")
        for url in mime.urls():
            if url.isLocalFile():
                low = url.toLocalFile().lower()
                if low.endswith(_IMAGE_EXTS + _COMPRESSED_EXTS):
                    return url
        return None

    def _browse(self) -> None:
        if self._browse_guard is not None and self._browse_guard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select image",
            settings.get("last_iso_dir") or "",
            "Disk image (*.iso *.img *.bin);;Compressed (*.zip *.gz *.xz *.zst);;All files (*)",
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
        dc = self._decompress_worker
        if dc is not None and dc.isRunning():
            dc.terminate()
            dc.wait(2000)
        self._decompress_worker = None
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
        self._cleanup_decompressed()

        dc = self._decompress_worker
        if dc is not None and dc.isRunning():
            dc.terminate()
            dc.wait(2000)
        self._decompress_worker = None

        from core.decompress import is_compressed

        self._path = path
        size = DriveDetector.format_size(os.path.getsize(path))
        self._iso_name.setText(os.path.basename(path))
        suffix = " (compressed)" if is_compressed(path) else ""
        self._iso_meta.setText(f"{size}{suffix} \u00b7 Verifying\u2026")
        self._iso_meta.setProperty("error", False)
        _restyle(self._iso_meta)
        self._loaded.setVisible(True)
        self._empty.setVisible(False)
        self.setProperty("loaded", True)
        _restyle(self)
        self.iso_selected.emit(path)

        if is_compressed(path):
            self._iso_meta.setText(f"{size} (compressed) \u00b7 Decompressing\u2026")
            import tempfile

            from core.decompress import compressed_format

            tmp_dir = tempfile.mkdtemp(prefix="flint-decompress-")
            fmt = compressed_format(path)
            if fmt in (".zip", ".gz", ".xz"):
                worker = DecompressWorker(path, tmp_dir, fmt)
                self._decompress_worker = worker
                worker.done.connect(self._on_decompress_done)
                worker.start()
            else:
                self._drop_error.setText(
                    f"Unsupported compression: {fmt}"
                )
                self._drop_error.setVisible(True)
                self._drop_timer.start(5000)
            return

        self._start_hash_and_analyze(path, size)

    def _on_decompress_done(
        self, path: str, ok: bool, extracted: str, tmp_dir: str
    ) -> None:
        if path != self._path:
            return
        self._decompress_worker = None
        if not ok:
            self._drop_error.setText(
                f"Failed to decompress {os.path.basename(path)}"
            )
            self._drop_error.setVisible(True)
            self._drop_timer.start(5000)
            return
        self._decompressed_path = extracted
        size = DriveDetector.format_size(os.path.getsize(path))
        self._start_hash_and_analyze(extracted, size)

    def _start_hash_and_analyze(self, hash_path: str, size: str) -> None:
        worker = IsoWorker(hash_path)
        self._worker = worker
        worker.hash_done.connect(self._on_hash_done)
        worker.progress.connect(self._on_hash_progress)
        worker.eta.connect(self._on_hash_eta)
        worker.start()

        analyzer = IsoDetectWorker(hash_path)
        self._analyzer = analyzer
        analyzer.detected.connect(self._on_analysis)
        analyzer.start()

    def _cleanup_decompressed(self) -> None:
        if self._decompressed_path is not None:
            try:
                os.unlink(self._decompressed_path)
            except OSError:
                pass
            tmp_dir = os.path.dirname(self._decompressed_path)
            if tmp_dir and "flint-decompress-" in tmp_dir:
                try:
                    os.rmdir(tmp_dir)
                except OSError:
                    pass
            self._decompressed_path = None

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
            self._iso_check.setText("\u2713\ufe0e")
            self._iso_check.setVisible(True)
        else:
            self._set_meta(False, f"{size} \u00b7 SHA256 failed")
            self._iso_check.setText("\u2715")
            self._iso_check.setVisible(True)
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
            button.setCursor(Qt.CursorShape.PointingHandCursor)
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

    def __init__(
        self,
        checked: bool = True,
        label: str = "",
        description: str = "",
    ) -> None:
        super().__init__()
        self.setObjectName("toggleSwitch")
        self._checked = checked
        self.setFixedSize(
            style.DESIGN_TOKENS["toggle_w"],
            style.DESIGN_TOKENS["toggle_h"],
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(label)
        self.setAccessibleDescription(description)

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

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 14, 14, 14)
        col.setSpacing(10)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(0)
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
        self._bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._written_stat = self._make_stat("Written")
        self._speed_stat = self._make_stat("Speed")
        self._eta_stat = self._make_stat("Remaining")
        stats = QHBoxLayout()
        stats.setContentsMargins(0, 0, 0, 0)
        stats.setSpacing(18)
        stats.addLayout(self._written_stat)
        stats.addLayout(self._speed_stat)
        stats.addLayout(self._eta_stat)
        stats.addStretch()

        self._error = QLabel("")
        self._error.setObjectName("progError")
        self._error.setWordWrap(True)
        self._error.setVisible(False)

        col.addLayout(head)
        col.addWidget(self._bar)
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
        self._smooth_timer.stop()
        self._error.setText(message)
        self._error.setProperty("level", "error")
        _restyle(self._error)
        self._error.setVisible(True)

    def set_warning(self, message: str) -> None:
        self._smooth_timer.stop()
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
        self._label = label

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
        self._label.setProperty("on", active)
        _restyle(self._label)
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
