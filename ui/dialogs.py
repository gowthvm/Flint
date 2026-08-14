"""Flint themed dialogs.

Every popup in the app goes through FlintDialog, a frameless QDialog
styled by the global QSS (QDialog#flintDialog rules in ui/style.py), so
completion notices, confirmations, information and text input all share
the app's design language. Callers never call exec() directly; use the
module-level helpers below.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

KIND_ICONS = {
    "success": "\u2713",
    "warning": "\u26a0",
    "error": "\u2715",
    "info": "\u2139",
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
