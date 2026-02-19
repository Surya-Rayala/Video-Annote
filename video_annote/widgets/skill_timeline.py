# video_annote/widgets/skill_timeline.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, QRect, QPoint, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QFontMetrics
from PyQt5.QtWidgets import (
    QWidget,
    QScrollArea,
    QMessageBox,
)

from ..domain import AnnotationRecord
from ..timeutils import Block, compute_lanes_for_annotations, ms_to_time_str


@dataclass
class _HitBlock:
    idx: int
    lane: int
    rect: QRect


class _SkillTimelineCanvas(QWidget):
    record_selected = pyqtSignal(int)  # index into annotations
    request_edit = pyqtSignal(int)     # user clicked Edit in dialog
    request_delete = pyqtSignal(int)   # user clicked Delete in dialog
    edit_preview = pyqtSignal(int, int, int)   # (idx, start_ms, end_ms)
    edit_preview_drag = pyqtSignal(int, int, int, str)  # (idx, start_ms, end_ms, edge) edge in {'left','right'}
    edit_committed = pyqtSignal(int, int, int) # (idx, start_ms, end_ms)
    edit_canceled = pyqtSignal(int)            # idx

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        # Data
        self._annotations: List[AnnotationRecord] = []
        self._lanes: List[List[Block]] = []
        self._duration_ms: int = 0

        # Styling/layout
        self._pad_x = 10
        self._pad_y = 10
        self._lane_h = 22
        self._lane_gap = 6
        self._block_h = 18
        self._handle_w = 8

        self._playhead_ms: int = 0

        # Color resolver: step_no -> "#RRGGBB"
        self._color_for_step: Callable[[int], str] = lambda n: "#00B400"

        # Selection / hit map
        self._hit_blocks: List[_HitBlock] = []
        self._selected_idx: Optional[int] = None

        # Editing state
        self._allow_edit: bool = True
        self._editing_idx: Optional[int] = None
        self._edit_start_ms: int = 0
        self._edit_end_ms: int = 0
        self._drag_mode: Optional[str] = None  # "left" | "right" | None

        self.setMouseTracking(True)
        self._cursor_mode: str = ""  # "arrow" | "hand" | "resize" | ""
    def _set_cursor_mode(self, mode: str) -> None:
        """Avoid repeatedly setting the same cursor (prevents flicker)."""
        mode = (mode or "").strip().lower()
        if mode == self._cursor_mode:
            return
        self._cursor_mode = mode
        if mode == "hand":
            self.setCursor(Qt.PointingHandCursor)
        elif mode == "resize":
            self.setCursor(Qt.SizeHorCursor)
        elif mode == "arrow":
            self.setCursor(Qt.ArrowCursor)
        else:
            self.unsetCursor()

    # ---------------- Public API ----------------

    def set_allow_edit(self, allow: bool) -> None:
        self._allow_edit = bool(allow)

    def set_duration_ms(self, duration_ms: int) -> None:
        self._duration_ms = max(0, int(duration_ms))
        self._resize_to_content()
        self.update()

    def set_color_resolver(self, fn: Callable[[int], str]) -> None:
        self._color_for_step = fn or (lambda n: "#00B400")
        self.update()

    def set_annotations(self, annotations: List[AnnotationRecord]) -> None:
        self._annotations = list(annotations or [])
        self._lanes = compute_lanes_for_annotations(self._annotations)
        # If selected idx out of range, clear selection
        if self._selected_idx is not None and not (0 <= self._selected_idx < len(self._annotations)):
            self._selected_idx = None
        # If editing idx out of range, cancel editing
        if self._editing_idx is not None and not (0 <= self._editing_idx < len(self._annotations)):
            self._editing_idx = None
            self._drag_mode = None
        self._resize_to_content()
        self.update()

    def set_playhead_ms(self, ms: int) -> None:
        self._playhead_ms = max(0, int(ms))
        self.update()

    def is_editing(self) -> bool:
        return self._editing_idx is not None

    def exit_editing(self, commit: bool = False) -> None:
        if self._editing_idx is None:
            return
        idx = self._editing_idx
        s = int(self._edit_start_ms)
        e = int(self._edit_end_ms)
        self._editing_idx = None
        self._drag_mode = None
        self.update()
        if commit:
            self.edit_committed.emit(idx, s, e)
        else:
            self.edit_canceled.emit(idx)

    # ---------------- Geometry helpers ----------------

    def _resize_to_content(self) -> None:
        lane_count = max(1, len(self._lanes))
        height = (
            self._pad_y * 2
            + lane_count * self._lane_h
            + (lane_count - 1) * self._lane_gap
        )
        # width should expand to parent viewport; set minimum for stability
        self.setMinimumHeight(height)
        self.setMinimumWidth(600)

    def _ms_to_x(self, ms: int) -> int:
        if self._duration_ms <= 0:
            return self._pad_x
        w = max(1, self.width() - 2 * self._pad_x)
        ms = max(0, min(int(ms), self._duration_ms))
        return self._pad_x + int(round((ms / self._duration_ms) * w))

    def _x_to_ms(self, x: int) -> int:
        if self._duration_ms <= 0:
            return 0
        w = max(1, self.width() - 2 * self._pad_x)
        rel = (int(x) - self._pad_x) / float(w)
        ms = int(round(rel * self._duration_ms))
        return max(0, min(ms, self._duration_ms))

    def _lane_top(self, lane_idx: int) -> int:
        return self._pad_y + lane_idx * (self._lane_h + self._lane_gap)

    # ---------------- Painting ----------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Background
        painter.fillRect(self.rect(), QColor("#141414"))

        # Content rect
        content = self.rect().adjusted(self._pad_x, self._pad_y, -self._pad_x, -self._pad_y)

        # Draw subtle border
        painter.setPen(QPen(QColor("#2b2b2b"), 1))
        painter.drawRect(content)

        # Clear hit map
        self._hit_blocks = []

        # Draw lanes + blocks
        fm = QFontMetrics(self.font())

        for lane_idx, lane in enumerate(self._lanes if self._lanes else [[]]):
            y_top = self._lane_top(lane_idx)
            # Lane baseline
            painter.setPen(QPen(QColor("#242424"), 1))
            painter.drawLine(self._pad_x, y_top + self._lane_h // 2, self.width() - self._pad_x, y_top + self._lane_h // 2)

            for b in lane:
                if not (0 <= b.idx < len(self._annotations)):
                    continue
                rec = self._annotations[b.idx]

                # Use live edit values for the block being edited so the timeline updates dynamically.
                if self._editing_idx is not None and b.idx == self._editing_idx:
                    start_ms = int(self._edit_start_ms)
                    end_ms = int(self._edit_end_ms)
                else:
                    start_ms = int(b.start_ms)
                    end_ms = int(b.end_ms)

                # Clamp to valid timeline range
                if self._duration_ms > 0:
                    start_ms = max(0, min(start_ms, self._duration_ms))
                    end_ms = max(0, min(end_ms, self._duration_ms))
                else:
                    start_ms = max(0, start_ms)
                    end_ms = max(0, end_ms)

                if end_ms < start_ms:
                    start_ms, end_ms = end_ms, start_ms

                x1 = self._ms_to_x(start_ms)
                x2 = self._ms_to_x(end_ms)
                if x2 <= x1:
                    x2 = x1 + 1

                rect = QRect(x1, y_top + (self._lane_h - self._block_h) // 2, x2 - x1, self._block_h)

                # Block fill
                color_hex = self._color_for_step(int(rec.step_no))
                c = QColor(color_hex)
                c.setAlpha(140)
                painter.fillRect(rect, c)

                # Block outline (selected / editing / normal)
                if self._editing_idx == b.idx:
                    pen = QPen(QColor("#ffffff"), 2)
                elif self._selected_idx == b.idx:
                    pen = QPen(QColor("#e6e6e6"), 2)
                else:
                    pen = QPen(QColor("#000000"), 1)
                painter.setPen(pen)
                painter.drawRect(rect)

                # Text label (step_no)
                painter.setPen(QPen(QColor("#0b0b0b"), 1))
                txt = str(rec.step_no)
                # Only draw if it fits
                if fm.horizontalAdvance(txt) + 6 < rect.width():
                    painter.drawText(rect.adjusted(3, 0, -3, 0), Qt.AlignVCenter | Qt.AlignLeft, txt)

                self._hit_blocks.append(_HitBlock(idx=b.idx, lane=lane_idx, rect=rect))

        # Draw edit handles if in edit mode
        if self._editing_idx is not None:
            idx = self._editing_idx
            # Find rect for editing block
            hb = next((h for h in self._hit_blocks if h.idx == idx), None)
            if hb is not None:
                # Left handle
                left_handle = QRect(hb.rect.left(), hb.rect.top(), self._handle_w, hb.rect.height())
                right_handle = QRect(hb.rect.right() - self._handle_w + 1, hb.rect.top(), self._handle_w, hb.rect.height())
                painter.fillRect(left_handle, QColor(255, 255, 255, 220))
                painter.fillRect(right_handle, QColor(255, 255, 255, 220))

                # Slight dim overlay outside edit range in that lane (optional visual)
                # (kept minimal to avoid clutter)

        # Draw playhead
        if self._duration_ms > 0:
            x = self._ms_to_x(self._playhead_ms)
            painter.setPen(QPen(QColor("#ff2d2d"), 2))
            painter.drawLine(x, self._pad_y, x, self.height() - self._pad_y)

        painter.end()

    # ---------------- Interaction / hit testing ----------------

    def _hit_test_block(self, pos: QPoint) -> Optional[_HitBlock]:
        for hb in self._hit_blocks:
            if hb.rect.contains(pos):
                return hb
        return None

    def _hit_test_handle(self, hb: _HitBlock, pos: QPoint) -> Optional[str]:
        if hb is None:
            return None
        left_handle = QRect(hb.rect.left(), hb.rect.top(), self._handle_w, hb.rect.height())
        right_handle = QRect(hb.rect.right() - self._handle_w + 1, hb.rect.top(), self._handle_w, hb.rect.height())
        if left_handle.contains(pos):
            return "left"
        if right_handle.contains(pos):
            return "right"
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        pos = event.pos()

        # If editing: handle dragging or commit if click outside handles
        if self._editing_idx is not None:
            hb = next((h for h in self._hit_blocks if h.idx == self._editing_idx), None)
            if hb:
                handle = self._hit_test_handle(hb, pos)
                if handle:
                    self._drag_mode = handle

                    # While dragging, snap the local playhead to the active edge so it visually follows.
                    if handle == "left":
                        self._playhead_ms = int(self._edit_start_ms)
                    else:
                        self._playhead_ms = int(self._edit_end_ms)

                    # Emit both signals: legacy (3 args) + new (with edge)
                    self.edit_preview.emit(self._editing_idx, int(self._edit_start_ms), int(self._edit_end_ms))
                    self.edit_preview_drag.emit(self._editing_idx, int(self._edit_start_ms), int(self._edit_end_ms), str(handle))

                    # Keep the edited block visible while starting a drag
                    parent = self.parent()
                    if parent is not None and hasattr(parent, "ensureVisible"):
                        cx = hb.rect.center().x()
                        cy = hb.rect.center().y()
                        try:
                            parent.ensureVisible(cx, cy, 60, 60)
                        except Exception:
                            pass

                    event.accept()
                    return
                # Click on same block but not on handle: do nothing (stay in edit)
                if hb.rect.contains(pos):
                    event.accept()
                    return

            # Click elsewhere confirms and exits edit mode
            self.exit_editing(commit=True)
            event.accept()
            return

        hb = self._hit_test_block(pos)
        if not hb:
            # click empty clears selection
            self._selected_idx = None
            self.update()
            return

        self._selected_idx = hb.idx
        self.record_selected.emit(hb.idx)
        self.update()
        self._set_cursor_mode("hand")

        # Show details dialog with Edit option
        self._show_block_info_dialog(hb.idx)
        event.accept()

    def mouseMoveEvent(self, event):
        pos = event.pos()

        # Cursor feedback
        if self._editing_idx is not None:
            # In edit mode: show resize cursor on handles, arrow otherwise.
            hb = next((h for h in self._hit_blocks if h.idx == self._editing_idx), None)
            if hb:
                handle = self._hit_test_handle(hb, pos)
                if handle:
                    self._set_cursor_mode("resize")
                else:
                    self._set_cursor_mode("arrow")
            else:
                self._set_cursor_mode("arrow")
        else:
            # Not editing: blocks are clickable (open dialog), so show pointing hand.
            hb_hover = self._hit_test_block(pos)
            if hb_hover is not None:
                self._set_cursor_mode("hand")
            else:
                self._set_cursor_mode("arrow")

        # Dragging handles
        if self._editing_idx is not None and self._drag_mode:
            hb = next((h for h in self._hit_blocks if h.idx == self._editing_idx), None)
            if not hb:
                return

            new_ms = self._x_to_ms(pos.x())

            if self._drag_mode == "left":
                self._edit_start_ms = min(new_ms, self._edit_end_ms)
            elif self._drag_mode == "right":
                self._edit_end_ms = max(new_ms, self._edit_start_ms)

            # While dragging, move the local playhead to the active edge so the view "follows".
            edge = str(self._drag_mode)
            if edge == "left":
                self._playhead_ms = int(self._edit_start_ms)
            else:
                self._playhead_ms = int(self._edit_end_ms)

            # Preview to main slider overlay (legacy) + edge-aware preview
            self.edit_preview.emit(self._editing_idx, int(self._edit_start_ms), int(self._edit_end_ms))
            self.edit_preview_drag.emit(self._editing_idx, int(self._edit_start_ms), int(self._edit_end_ms), edge)

            # Auto-scroll vertically to keep the edited block visible if there are many lanes
            parent = self.parent()
            if parent is not None and hasattr(parent, "ensureVisible"):
                # Re-hit the current block rect; if it exists, keep its center visible.
                hb2 = next((h for h in self._hit_blocks if h.idx == self._editing_idx), None)
                if hb2 is not None:
                    try:
                        parent.ensureVisible(hb2.rect.center().x(), hb2.rect.center().y(), 60, 60)
                    except Exception:
                        pass

            self.update()
            return

        return super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        # Reset cursor when leaving the canvas.
        self._set_cursor_mode("arrow")
        return super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_mode:
            self._drag_mode = None
            event.accept()
            return
        return super().mouseReleaseEvent(event)

    # ---------------- Dialog + edit entry ----------------

    def _show_block_info_dialog(self, idx: int) -> None:
        if not (0 <= idx < len(self._annotations)):
            return
        rec = self._annotations[idx]
        s_ms = int(round(rec.start_time * 1000.0))
        e_ms = int(round(rec.end_time * 1000.0))

        msg = (
            f"Step {rec.step_no}: {rec.step_name}\n\n"
            f"Start: {ms_to_time_str(s_ms)} ({rec.start_time:.3f}s)\n"
            f"End:   {ms_to_time_str(e_ms)} ({rec.end_time:.3f}s)\n"
            f"Total: {rec.total_time:.3f}s\n\n"
            f"Time source: {rec.time_source}\n"
            f"Audio source: {rec.audio_source}\n"
            f"Confidence: {int(rec.confidence)}\n"
        )

        box = QMessageBox(self)
        box.setWindowTitle("Annotation")
        box.setText(msg)
        box.setIcon(QMessageBox.Information)

        btn_close = box.addButton("Close", QMessageBox.RejectRole)
        btn_edit = box.addButton("Edit", QMessageBox.AcceptRole)
        btn_delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        if not self._allow_edit:
            btn_edit.setEnabled(False)
            btn_delete.setEnabled(False)

        box.exec_()
        clicked = box.clickedButton()
        if clicked == btn_edit and self._allow_edit:
            self._enter_edit_mode(idx)
        elif clicked == btn_delete and self._allow_edit:
            # MainWindow handles confirmation + deletion; we just emit the request here.
            # If currently editing this block, exit editing without commit before deleting.
            if self._editing_idx is not None and self._editing_idx == idx:
                self.exit_editing(commit=False)
            self.request_delete.emit(idx)

    def _enter_edit_mode(self, idx: int) -> None:
        if not (0 <= idx < len(self._annotations)):
            return
        rec = self._annotations[idx]
        self._editing_idx = idx
        self._selected_idx = idx
        self._edit_start_ms = int(round(rec.start_time * 1000.0))
        self._edit_end_ms = int(round(rec.end_time * 1000.0))
        self._drag_mode = None

        # Tell main window we want to edit this record (so it can enforce "no unfinished step" etc.)
        self.request_edit.emit(idx)
        # Also emit a first preview to show overlay on main slider if desired
        self.edit_preview.emit(idx, int(self._edit_start_ms), int(self._edit_end_ms))
        # Edge-aware preview: default to right edge (common when trimming end)
        self.edit_preview_drag.emit(idx, int(self._edit_start_ms), int(self._edit_end_ms), "right")
        self.update()


class SkillTimeline(QScrollArea):
    """
    Scrollable Timeline view (stacked blocks + playhead).

    Use:
      - set_duration_ms(duration_ms) to match the time-source duration / slider max
      - set_annotations(records)
      - set_playhead_ms(current_ms)
      - set_allow_edit(True/False) depending on "no unfinished step" policy

    Signals are forwarded from the canvas.
    """
    record_selected = pyqtSignal(int)
    request_edit = pyqtSignal(int)
    request_delete = pyqtSignal(int)
    edit_preview = pyqtSignal(int, int, int)
    edit_preview_drag = pyqtSignal(int, int, int, str)
    edit_committed = pyqtSignal(int, int, int)
    edit_canceled = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.canvas = _SkillTimelineCanvas(self)
        self.setWidget(self.canvas)

        # Forward signals
        self.canvas.record_selected.connect(self.record_selected.emit)
        self.canvas.request_edit.connect(self.request_edit.emit)
        self.canvas.request_delete.connect(self.request_delete.emit)
        self.canvas.edit_preview.connect(self.edit_preview.emit)
        self.canvas.edit_preview_drag.connect(self.edit_preview_drag.emit)
        self.canvas.edit_committed.connect(self.edit_committed.emit)
        self.canvas.edit_canceled.connect(self.edit_canceled.emit)

        # Presentational border/title can be done by parent container; keep this minimal.

    def set_allow_edit(self, allow: bool) -> None:
        self.canvas.set_allow_edit(allow)

    def set_duration_ms(self, duration_ms: int) -> None:
        self.canvas.set_duration_ms(duration_ms)

    def set_color_resolver(self, fn: Callable[[int], str]) -> None:
        self.canvas.set_color_resolver(fn)

    def set_annotations(self, annotations: List[AnnotationRecord]) -> None:
        self.canvas.set_annotations(annotations)

    def set_playhead_ms(self, ms: int) -> None:
        self.canvas.set_playhead_ms(ms)

    def is_editing(self) -> bool:
        return self.canvas.is_editing()

    def exit_editing(self, commit: bool = False) -> None:
        self.canvas.exit_editing(commit=commit)