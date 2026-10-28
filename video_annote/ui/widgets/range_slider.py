# video_annote/ui/widgets/range_slider.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from PyQt5.QtCore import Qt, QRect, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QSlider, QStyle, QStyleOptionSlider


@dataclass
class OverlayRange:
    """
    A translucent overlay on the slider groove, in slider value units (ms).
    """
    start_value: int
    end_value: int
    color_hex: str = "#00B400"
    alpha: int = 120


class RangeOverlaySlider(QSlider):
    """
    A QSlider that draws 0..N colored overlay ranges on the groove.

    Safety:
      - A click off the handle only asks to go there (``seek_requested``) while ``click_to_seek`` is
        set; the main window clears it while a label is being recorded, so a stray click cannot move
        the frame Confirm Start/End would take
      - Ignores mouse wheel (prevents accidental timeline changes)
      - Takes no keyboard focus: keyboard steps would move the handle without seeking the players
        (frame stepping is offered by the main window's shortcuts instead)
    """
    seek_requested = pyqtSignal(int)  # slider value (ms) clicked off the handle

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.click_to_seek = False

        self._overlays: List[OverlayRange] = []

        # Optional: an explicit "edit" overlay handle range; UI can set it separately
        self._edit_overlay: Optional[OverlayRange] = None

        # Cursor feedback: show pointing hand when hovering the handle.
        self.setMouseTracking(True)
        self._cursor_on_handle = False
        self.setFocusPolicy(Qt.NoFocus)

    # -------------
    # Overlay API
    # -------------

    def set_overlays(self, overlays: List[OverlayRange]) -> None:
        self._overlays = list(overlays or [])
        self.update()

    def clear_overlays(self) -> None:
        self._overlays = []
        self._edit_overlay = None
        self.update()

    def set_edit_overlay(self, start_value: int, end_value: int, color_hex: str, alpha: int = 160) -> None:
        self._edit_overlay = OverlayRange(start_value=int(start_value), end_value=int(end_value), color_hex=color_hex, alpha=alpha)
        self.update()

    def clear_edit_overlay(self) -> None:
        self._edit_overlay = None
        self.update()

    # -------------
    # Interaction safety
    # -------------

    def _click_is_on_handle(self, pos) -> bool:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        return handle.contains(pos)

    def _value_at(self, pos) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), pos.x() - groove.x() - handle.width() // 2,
                                              max(1, groove.width() - handle.width()))

    def _update_hover_cursor(self, pos) -> None:
        """Update cursor to indicate clickability: the handle, and the whole bar while a click goes there."""
        try:
            on_handle = self._click_is_on_handle(pos) or (self.click_to_seek and self.maximum() > self.minimum())
        except Exception:
            on_handle = False

        if on_handle and not self._cursor_on_handle:
            self._cursor_on_handle = True
            self.setCursor(Qt.PointingHandCursor)
        elif (not on_handle) and self._cursor_on_handle:
            self._cursor_on_handle = False
            self.unsetCursor()

    def mousePressEvent(self, event):
        # only the left button; off the handle it goes there only when allowed (see the class notes), and
        # dragging the handle always works. (Other buttons would make QSlider jump to the click.)
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        if not self._click_is_on_handle(event.pos()):
            self._update_hover_cursor(event.pos())
            if self.click_to_seek and self.maximum() > self.minimum():
                self.seek_requested.emit(self._value_at(event.pos()))
                event.accept()
            else:
                event.ignore()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        event.ignore()

    def mouseMoveEvent(self, event):
        # Cursor feedback while hovering the widget.
        self._update_hover_cursor(event.pos())
        return super().mouseMoveEvent(event)

    def enterEvent(self, event):
        # When entering, update cursor immediately.
        try:
            pos = self.mapFromGlobal(self.cursor().pos())
            self._update_hover_cursor(pos)
        except Exception:
            pass
        return super().enterEvent(event)

    def leaveEvent(self, event):
        # Reset cursor when leaving the widget.
        if getattr(self, "_cursor_on_handle", False):
            self._cursor_on_handle = False
            self.unsetCursor()
        return super().leaveEvent(event)

    # -------------
    # Painting
    # -------------

    def _value_to_pixel(self, value: int, groove: QRect) -> int:
        """Map a slider value to an x (or y) pixel position within the groove."""
        if self.maximum() <= self.minimum():
            return groove.x() if self.orientation() == Qt.Horizontal else groove.y()

        v = int(value)
        v = max(self.minimum(), min(v, self.maximum()))

        if self.orientation() == Qt.Horizontal:
            span = max(1, groove.width())
            p = QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), v, span)
            return groove.x() + p
        else:
            span = max(1, groove.height())
            p = QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), v, span)
            return groove.y() + p

    def _paint_overlay(self, painter: QPainter, overlay: OverlayRange, groove: QRect) -> None:
        if overlay is None:
            return
        if self.maximum() <= self.minimum():
            return

        s = int(overlay.start_value)
        e = int(overlay.end_value)
        if e < s:
            s, e = e, s

        s = max(self.minimum(), min(s, self.maximum()))
        e = max(self.minimum(), min(e, self.maximum()))
        if e < s:
            return

        if self.orientation() == Qt.Horizontal:
            x1 = self._value_to_pixel(s, groove)
            x2 = self._value_to_pixel(e, groove)
            x_left = min(x1, x2)
            x_right = max(x1, x2)

            h = max(4, groove.height())
            y = groove.center().y() - (h // 2)
            rect = QRect(x_left, y, max(0, x_right - x_left), h)
        else:
            y1 = self._value_to_pixel(s, groove)
            y2 = self._value_to_pixel(e, groove)
            y_top = min(y1, y2)
            y_bot = max(y1, y2)

            w = max(4, groove.width())
            x = groove.center().x() - (w // 2)
            rect = QRect(x, y_top, w, max(0, y_bot - y_top))

        if rect.isNull() or rect.width() == 0 or rect.height() == 0:
            return

        c = QColor(overlay.color_hex)
        c.setAlpha(int(max(0, min(overlay.alpha, 255))))
        painter.fillRect(rect, c)

    def paintEvent(self, event):
        # Draw the base slider (groove, handle, tick marks)
        super().paintEvent(event)

        if not self._overlays and self._edit_overlay is None:
            return

        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        if groove.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Paint persistent overlays first
        for ov in self._overlays:
            self._paint_overlay(painter, ov, groove)

        # Paint edit overlay on top if present
        if self._edit_overlay is not None:
            self._paint_overlay(painter, self._edit_overlay, groove)

        painter.end()