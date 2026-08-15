"""Flint themed dialogs.

Every popup in the app goes through FlintDialog, a frameless QDialog
styled by the global QSS (QDialog#flintDialog rules in ui/style.py), so
completion notices, confirmations, information and text input all share
the app's design language. Callers never call exec() directly; use the
module-level helpers below.
"""

from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QUrl
from PyQt6.QtGui import QFont, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core import settings
from ui.style import palette

KIND_ICONS = {
    "success": "\u2713\ufe0e",
    "warning": "\u26a0\ufe0e",
    "error": "\u2715",
    "info": "\u2139\ufe0e",
}


class FlintDialog(QDialog):
    """Frameless themed popup.

    buttons: (label, style, result) triples; style is a QSS objectName
    ("primary", "ghost", "danger"). run() executes modally and returns
    the clicked button's result (None on Esc). check_text optionally
    adds a checkbox (read via checked()); input_placeholder adds a text
    field (read via field_text()).
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        kind: str = "info",
        title: str = "",
        message: str = "",
        buttons: list[tuple[str, str, str]] | None = None,
        check_text: str | None = None,
        input_placeholder: str | None = None,
        mono: bool = False,
    ) -> None:
        super().__init__(parent)
        self._result: str | None = None
        self._check: QCheckBox | None = None
        self._field: QLineEdit | None = None
        self.setObjectName("flintDialog")
        # The frameless window has no native title bar; still expose the
        # title for accessibility (screen readers, task switching).
        self.setWindowTitle(title or "Flint")
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setModal(True)
        self.setMinimumWidth(380)
        self.setMaximumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        heading = QHBoxLayout()
        heading.setSpacing(10)
        icon = QLabel(KIND_ICONS.get(kind, KIND_ICONS["info"]))
        icon.setObjectName("flintDialogIcon")
        icon.setProperty("dialogRole", kind)
        icon.setFixedWidth(30)
        icon.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        title_label = QLabel(title)
        title_label.setObjectName("flintDialogTitle")
        title_label.setWordWrap(True)
        heading.addWidget(icon)
        heading.addWidget(title_label, 1)
        root.addLayout(heading)

        body = QVBoxLayout()
        body.setSpacing(10)
        if message:
            msg = QLabel(message)
            msg.setObjectName("flintDialogMessage")
            msg.setWordWrap(True)
            msg.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            if mono:
                msg.setFont(QFont("Cascadia Mono", 9))
            body.addWidget(msg)
        if input_placeholder is not None:
            self._field = QLineEdit()
            self._field.setObjectName("flintDialogInput")
            self._field.setPlaceholderText(input_placeholder)
            body.addWidget(self._field)
        if check_text:
            self._check = QCheckBox(check_text)
            self._check.setObjectName("flintDialogCheck")
            body.addWidget(self._check)
        root.addLayout(body)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        for label, style_name, result in buttons or []:
            btn = QPushButton(label)
            btn.setObjectName(style_name)
            btn.setMinimumWidth(90)
            btn.clicked.connect(
                lambda _=False, r=result: self._accept(r)
            )
            if style_name == "primary":
                btn.setDefault(True)
            row.addWidget(btn)
        root.addLayout(row)

    def _accept(self, result: str) -> None:
        self._result = result
        self.accept()

    def run(self) -> str | None:
        """Execute modally and return the clicked button's result."""
        self._result = None
        self.adjustSize()
        self.exec()
        return self._result

    def checked(self) -> bool:
        return bool(self._check is not None and self._check.isChecked())

    def field_text(self) -> str:
        return self._field.text() if self._field is not None else ""


def completion(
    parent: QWidget,
    *,
    kind: str,
    title: str,
    message: str,
    buttons: list[tuple[str, str, str]] | None = None,
) -> str | None:
    """Outcome popup shown when a flash, verify or wipe finishes."""
    if buttons is None:
        buttons = [
            ("Eject drive", "ghost", "eject"),
            ("Copy report", "ghost", "copy"),
            ("Close", "primary", "close"),
        ]
    return FlintDialog(
        parent, kind=kind, title=title, message=message, buttons=buttons
    ).run()


def confirm(
    parent: QWidget,
    *,
    kind: str = "warning",
    title: str,
    message: str,
    accept: str,
    reject: str = "Cancel",
    accept_style: str = "primary",
) -> bool:
    """Two-button confirmation; True when the accept button is clicked."""
    dlg = FlintDialog(
        parent,
        kind=kind,
        title=title,
        message=message,
        buttons=[
            (reject, "ghost", "no"),
            (accept, accept_style, "yes"),
        ],
    )
    return dlg.run() == "yes"


def inform(
    parent: QWidget,
    *,
    kind: str = "info",
    title: str,
    message: str,
    check: str | None = None,
) -> FlintDialog:
    """Single-button information popup; returns the dialog so callers can
    read .checked() afterwards."""
    dlg = FlintDialog(
        parent,
        kind=kind,
        title=title,
        message=message,
        buttons=[("Close", "primary", "close")],
        check_text=check,
    )
    dlg.run()
    return dlg


class _DragBar(QWidget):
    """Header strip that drags its parent frameless dialog."""

    def __init__(self, dialog: QDialog) -> None:
        super().__init__(dialog)
        self._dlg = dialog
        self._drag_pos: QPoint | None = None

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self._dlg.frameGeometry().topLeft()
            )
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if (
            self._drag_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._dlg.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class HelpDialog(QDialog):
    """In-app viewer for the reference manual.

    Renders ui/reference.html in a QTextBrowser so the manual never
    leaves the app; external links are intentionally inert.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("helpDialog")
        self.setWindowTitle("Flint reference")
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setModal(False)
        self.setMinimumSize(620, 400)
        self.resize(820, 580)
        self._loaded = False
        self._path = Path(__file__).resolve().parent / "reference.html"

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        bar = _DragBar(self)
        head = QHBoxLayout(bar)
        head.setContentsMargins(2, 2, 2, 2)
        head.setSpacing(8)
        title = QLabel("Reference")
        title.setObjectName("helpDialogTitle")
        head.addWidget(title)
        head.addStretch(1)
        self._back_btn = QPushButton("\u2190")
        self._back_btn.setObjectName("helpNavBtn")
        self._back_btn.setFixedSize(30, 24)
        self._back_btn.setEnabled(False)
        self._fwd_btn = QPushButton("\u2192")
        self._fwd_btn.setObjectName("helpNavBtn")
        self._fwd_btn.setFixedSize(30, 24)
        self._fwd_btn.setEnabled(False)
        close_btn = QPushButton("\u2715")
        close_btn.setObjectName("ghost")
        close_btn.setFixedSize(30, 24)
        close_btn.clicked.connect(self.close)
        head.addWidget(self._back_btn)
        head.addWidget(self._fwd_btn)
        head.addWidget(close_btn)
        root.addWidget(bar)

        self._view = QTextBrowser()
        self._view.setObjectName("helpView")
        self._view.setOpenExternalLinks(False)
        self._view.setOpenLinks(True)
        self._view.backwardAvailable.connect(self._back_btn.setEnabled)
        self._view.forwardAvailable.connect(self._fwd_btn.setEnabled)
        self._back_btn.clicked.connect(self._view.backward)
        self._fwd_btn.clicked.connect(self._view.forward)

        theme = str(settings.get("theme") or "dark")
        doc = self._view.document()
        if doc is not None:
            doc.setDefaultStyleSheet(
                f"a {{ color: {palette(theme)['primary']}; }}"
            )
        root.addWidget(self._view, 1)

    def show_anchor(self, anchor: str) -> None:
        if not self._loaded:
            self._view.setSource(QUrl.fromLocalFile(str(self._path)))
            self._loaded = True
        if anchor:
            self._view.setSource(QUrl(f"#{anchor}"))
        self._view.setFocus()

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        assert event is not None
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


def input_text(
    parent: QWidget,
    *,
    title: str,
    message: str,
    placeholder: str = "",
) -> tuple[str, bool]:
    """Prompt for a line of text; returns (text, accepted)."""
    dlg = FlintDialog(
        parent,
        kind="info",
        title=title,
        message=message,
        input_placeholder=placeholder,
        buttons=[
            ("Cancel", "ghost", "no"),
            ("Confirm", "primary", "yes"),
        ],
    )
    ok = dlg.run() == "yes"
    return dlg.field_text(), ok
