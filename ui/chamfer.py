"""Chamfered-corner panels: the signature cut-corner motif of the Flint UI.

A ChamferPanel paints a box whose corners are cut diagonally instead of
rounded, echoing the emblem's bar -> chamfer -> block geometry. It paints
its own background/border (hover and focus states included), so matching
QSS rules for the same objectName only contribute box-model padding and
child layouts stay identical.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QRectF
from PyQt6.QtGui import QColor, QEnterEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PyQt6.QtWidgets import QFrame

from ui.style import DESIGN_TOKENS, palette_for


def chamfer_path(rect: QRectF, cut: float) -> QPainterPath:
    """Rectangular path with all four corners cut diagonally by ``cut`` px."""
    cut = min(cut, rect.width() / 2, rect.height() / 2)
    path = QPainterPath()
    path.moveTo(rect.left() + cut, rect.top())
    path.lineTo(rect.right() - cut, rect.top())
    path.lineTo(rect.right(), rect.top() + cut)
    path.lineTo(rect.right(), rect.bottom() - cut)
    path.lineTo(rect.right() - cut, rect.bottom())
    path.lineTo(rect.left() + cut, rect.bottom())
    path.lineTo(rect.left(), rect.bottom() - cut)
    path.lineTo(rect.left(), rect.top() + cut)
    path.closeSubpath()
    return path


class ChamferPanel(QFrame):
    """QFrame painting a chamfered-corner box with hover/focus borders.

    Instantiate with ``cut`` pixels (defaults to the ``chamfer`` design
    token). Paint mode ignores the QSS background/border of the widget
    itself; descendants keep their own QSS styling.
    """

    def __init__(self, cut: int | None = None) -> None:
        super().__init__()
        self._cut = float(cut if cut is not None else DESIGN_TOKENS["chamfer"])
        self._hovered = False

    @property
    def cut(self) -> float:
        """Chamfer size in pixels."""
        return self._cut

    @cut.setter
    def cut(self, value: float) -> None:
        self._cut = value
        self.update()

    def enterEvent(self, event: QEnterEvent | None) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent | None) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent | None) -> None:
        palette = palette_for()
        path = chamfer_path(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._cut
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(
            path, QColor(palette["track"] if self._hovered else palette["card"])
        )
        active = self._hovered or self.hasFocus()
        painter.setPen(QPen(QColor(palette["muted"] if active else palette["border"]), 1.0))
        painter.drawPath(path)
        painter.end()