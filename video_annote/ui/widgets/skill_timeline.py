# video_annote/ui/widgets/skill_timeline.py
"""Timeline of annotation blocks (stacked in lanes when they overlap) with a playhead and
drag-to-edit handles. Records are referred to by object identity, never by list position, so
edits cannot land on a different record when the list changes underneath."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from PyQt5.QtCore import QEvent, QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QMessageBox, QScrollArea, QSizePolicy, QToolTip, QWidget

from ...core.models import AnnotationRecord
from ...core.timecode import Block, compute_lanes_for_annotations, ms_to_precise_str, ms_to_time_str

RULER_STEPS_S = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


def _text_color(fill: QColor) -> QColor:
    """Black or white, whichever reads better on ``fill``."""
    luminance = 0.2126 * fill.red() + 0.7152 * fill.green() + 0.0722 * fill.blue()
    return QColor("#000000") if luminance > 140 else QColor("#ffffff")


@dataclass
class _HitBlock:
    record: AnnotationRecord
    rect: QRect


class _TimelineCanvas(QWidget):
    record_clicked = pyqtSignal(object)  # record, or None for empty space (a double-click opens its details)
    request_edit = pyqtSignal(object)  # record
    request_delete = pyqtSignal(object)  # record
    edit_preview = pyqtSignal(object, int, int, str)  # record, start_ms, end_ms, active edge
    edit_committed = pyqtSignal(object, int, int)  # record, start_ms, end_ms
    edit_canceled = pyqtSignal(object)  # record
    drag_started = pyqtSignal()
    drag_finished = pyqtSignal()

    PAD_X, PAD_Y, LANE_GAP, HANDLE_W = 10, 6, 4, 8

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # blocks fit their text at any font size (e.g. Windows at 150 %)
        self.BLOCK_H = max(18, self.fontMetrics().height() + 4)
        self.LANE_H = self.BLOCK_H + 4
        self._ruler_font = QFont(self.font())
        self._ruler_font.setPointSizeF(max(7.0, self.font().pointSizeF() - 2))
        self.RULER_H = QFontMetrics(self._ruler_font).height() + 6
        self._annotations: List[AnnotationRecord] = []
        self._lanes: List[List[Block]] = []
        self._duration_ms = 0
        self._playhead_ms = 0
        self._color_for_step: Callable[[int], str] = lambda n: "#00B400"
        self._hits: List[_HitBlock] = []
        self._selected: Optional[AnnotationRecord] = None
        self._allow_edit = True
        self._editing: Optional[AnnotationRecord] = None
        self._edit_start_ms = 0
        self._edit_end_ms = 0
        self._drag_edge: Optional[str] = None
        self._cursor_mode = ""
        self.setMouseTracking(True)
        self._resize_to_content()

    # ---------------- public API ----------------

    def set_allow_edit(self, allow: bool) -> None:
        self._allow_edit = bool(allow)

    def set_duration_ms(self, duration_ms: int) -> None:
        self._duration_ms = max(0, int(duration_ms))
        self.update()

    def set_color_resolver(self, fn: Callable[[int], str]) -> None:
        self._color_for_step = fn or (lambda n: "#00B400")
        self.update()

    def set_annotations(self, annotations: List[AnnotationRecord]) -> None:
        self._annotations = list(annotations or [])
        self._lanes = compute_lanes_for_annotations(self._annotations)
        if self._selected is not None and not self._contains(self._selected):
            self._selected = None
        if self._editing is not None and not self._contains(self._editing):
            self._editing, self._drag_edge = None, None  # the record is gone: drop the edit
        self._resize_to_content()
        self.update()

    def set_playhead_ms(self, ms: int) -> None:
        ms = max(0, int(ms))
        if ms != self._playhead_ms:
            self._playhead_ms = ms
            self.update()

    def set_selected(self, rec: Optional[AnnotationRecord]) -> None:
        rec = rec if rec is not None and self._contains(rec) else None
        if rec is not self._selected:
            self._selected = rec
            self.update()

    def selected_record(self) -> Optional[AnnotationRecord]:
        return self._selected

    def editing_record(self) -> Optional[AnnotationRecord]:
        return self._editing

    def exit_editing(self, commit: bool = False) -> None:
        rec = self._editing
        if rec is None:
            return
        start, end = int(self._edit_start_ms), int(self._edit_end_ms)
        dragging = self._drag_edge is not None  # (Esc/Return while a handle is still held)
        self._editing, self._drag_edge = None, None
        self.update()
        if dragging:
            self.drag_finished.emit()
        if commit:
            self.edit_committed.emit(rec, start, end)
        else:
            self.edit_canceled.emit(rec)

    # ---------------- geometry ----------------

    def _contains(self, rec: AnnotationRecord) -> bool:
        return any(r is rec for r in self._annotations)

    def height_for_lanes(self, lanes: int) -> int:
        lanes = max(1, lanes)
        return self.PAD_Y * 2 + lanes * self.LANE_H + (lanes - 1) * self.LANE_GAP + self.RULER_H

    def _resize_to_content(self) -> None:
        self.setMinimumHeight(self.height_for_lanes(len(self._lanes)))
        self.setMinimumWidth(240)

    def _ms_to_x(self, ms: int) -> int:
        if self._duration_ms <= 0:
            return self.PAD_X
        w = max(1, self.width() - 2 * self.PAD_X)
        ms = max(0, min(int(ms), self._duration_ms))
        return self.PAD_X + int(round(ms / self._duration_ms * w))

    def _x_to_ms(self, x: int) -> int:
        if self._duration_ms <= 0:
            return 0
        w = max(1, self.width() - 2 * self.PAD_X)
        return max(0, min(int(round((x - self.PAD_X) / w * self._duration_ms)), self._duration_ms))

    def _lane_top(self, lane: int) -> int:
        return self.PAD_Y + lane * (self.LANE_H + self.LANE_GAP)

    def _hit_for(self, rec: Optional[AnnotationRecord]) -> Optional[_HitBlock]:
        return next((h for h in self._hits if h.record is rec), None) if rec is not None else None

    def _handle_at(self, hit: _HitBlock, pos: QPoint) -> Optional[str]:
        r = hit.rect
        if QRect(r.left(), r.top(), self.HANDLE_W, r.height()).contains(pos):
            return "left"
        if QRect(r.right() - self.HANDLE_W + 1, r.top(), self.HANDLE_W, r.height()).contains(pos):
            return "right"
        return None

    # ---------------- painting ----------------

    def paintEvent(self, event):  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#141414"))
        fm = QFontMetrics(self.font())
        self._hits = []

        for lane_idx, lane in enumerate(self._lanes or [[]]):
            top = self._lane_top(lane_idx)
            painter.setPen(QPen(QColor("#2a2a2a"), 1))
            painter.drawLine(self.PAD_X, top + self.LANE_H // 2, self.width() - self.PAD_X, top + self.LANE_H // 2)
            for b in lane:
                if not 0 <= b.idx < len(self._annotations):
                    continue
                rec = self._annotations[b.idx]
                if rec is self._editing:
                    start, end = self._edit_start_ms, self._edit_end_ms
                else:
                    start, end = b.start_ms, b.end_ms
                x1, x2 = self._ms_to_x(min(start, end)), self._ms_to_x(max(start, end))
                rect = QRect(x1, top + (self.LANE_H - self.BLOCK_H) // 2, max(2, x2 - x1), self.BLOCK_H)
                fill = QColor(self._color_for_step(int(rec.step_no)))
                painter.fillRect(rect, fill)  # opaque: every label color stays readable on the dark background
                if rec is self._editing or rec is self._selected:
                    painter.setPen(QPen(QColor("#ffffff"), 2))
                else:  # a faint light edge, so that dark colors (navy, maroon) still stand out
                    painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
                painter.drawRect(rect.adjusted(0, 0, -1, -1))
                number, room = str(rec.step_no), rect.width() - 6
                text = fm.elidedText(f"{number} {rec.step_name}", Qt.ElideRight, room)
                if not text.startswith(number) or len(text) <= len(number) + 1:  # the name does not fit
                    text = number if fm.horizontalAdvance(number) <= room else ""
                if text:
                    painter.setPen(QPen(_text_color(fill), 1))
                    painter.drawText(rect.adjusted(3, 0, -3, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
                self._hits.append(_HitBlock(rec, rect))

        self._paint_ruler(painter)

        hit = self._hit_for(self._editing)
        if hit is not None:
            r = hit.rect
            painter.fillRect(QRect(r.left(), r.top(), self.HANDLE_W, r.height()), QColor(255, 255, 255, 220))
            painter.fillRect(QRect(r.right() - self.HANDLE_W + 1, r.top(), self.HANDLE_W, r.height()),
                             QColor(255, 255, 255, 220))

        if self._duration_ms > 0:
            x = self._ms_to_x(self._playhead_ms)
            painter.setPen(QPen(QColor("#ff2d2d"), 2))
            painter.drawLine(x, self.PAD_Y, x, self.height() - self.PAD_Y)
        painter.end()

    def _paint_ruler(self, painter: QPainter) -> None:
        """Minute:second marks along the bottom, as far apart as their labels need."""
        if self._duration_ms <= 0:
            return
        painter.setFont(self._ruler_font)
        fm = QFontMetrics(self._ruler_font)
        label_w = fm.horizontalAdvance("00:00") + 14
        px_per_s = max(1, self.width() - 2 * self.PAD_X) / (self._duration_ms / 1000.0)
        step = next((t for t in RULER_STEPS_S if t * px_per_s >= label_w), RULER_STEPS_S[-1])
        y = self.height() - self.PAD_Y - self.RULER_H
        painter.setPen(QPen(QColor("#2a2a2a"), 1))
        painter.drawLine(self.PAD_X, y, self.width() - self.PAD_X, y)
        painter.setPen(QPen(QColor("#8a8a8a"), 1))
        t = 0
        while t * 1000 <= self._duration_ms:
            x = self._ms_to_x(t * 1000)
            painter.drawLine(x, y, x, y + 3)
            box = QRect(x - label_w // 2, y + 3, label_w, self.RULER_H - 3)
            align = Qt.AlignHCenter
            if box.left() < 0:
                box.moveLeft(x)
                align = Qt.AlignLeft
            elif box.right() > self.width():
                box.moveRight(x)
                align = Qt.AlignRight
            painter.drawText(box, align | Qt.AlignTop, ms_to_time_str(t * 1000))
            t += step
        painter.setFont(self.font())

    # ---------------- interaction ----------------

    def _set_cursor(self, mode: str) -> None:
        if mode == self._cursor_mode:
            return
        self._cursor_mode = mode
        self.setCursor({"hand": Qt.PointingHandCursor, "resize": Qt.SizeHorCursor}.get(mode, Qt.ArrowCursor))

    def mousePressEvent(self, event):  # noqa: N802 (Qt API)
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        pos = event.pos()
        if self._editing is not None:
            hit = self._hit_for(self._editing)
            if hit is not None:
                edge = self._handle_at(hit, pos)
                if edge:
                    self._drag_edge = edge
                    self.drag_started.emit()
                    self._emit_preview()
                    event.accept()
                    return
                if hit.rect.contains(pos):
                    event.accept()
                    return
            self.exit_editing(commit=True)  # click elsewhere confirms the edit
            event.accept()
            return
        hit = next((h for h in self._hits if h.rect.contains(pos)), None)
        self._selected = hit.record if hit else None
        self.update()
        self.record_clicked.emit(hit.record if hit else None)  # None: nothing selected any more
        event.accept()

    def mouseDoubleClickEvent(self, event):  # noqa: N802 (Qt API)
        if event.button() != Qt.LeftButton or self._editing is not None:
            return super().mouseDoubleClickEvent(event)
        hit = next((h for h in self._hits if h.rect.contains(event.pos())), None)
        if hit is not None:
            self._show_block_dialog(hit.record)
        event.accept()

    def event(self, event):  # noqa: N802 (Qt API)
        if event.type() == QEvent.ToolTip:
            hit = next((h for h in self._hits if h.rect.contains(event.pos())), None)
            if hit is None:
                QToolTip.hideText()
                event.ignore()
            else:
                rec = hit.record
                QToolTip.showText(event.globalPos(), (
                    f"Step {rec.step_no}: {rec.step_name}\n"
                    f"{ms_to_precise_str(int(round(rec.start_time * 1000)))} → "
                    f"{ms_to_precise_str(int(round(rec.end_time * 1000)))}  ({rec.total_time:.3f} s)\n"
                    f"Confidence {int(rec.confidence)}" + (f"\n{rec.notes.splitlines()[0][:80]}" if rec.notes else "")
                    + "\n\nClick: show its first frame · double-click: details, edit, delete"), self)
            return True
        return super().event(event)

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt API)
        pos = event.pos()
        if self._editing is not None and self._drag_edge:
            ms = self._x_to_ms(pos.x())
            if self._drag_edge == "left":
                self._edit_start_ms = min(ms, self._edit_end_ms)
            else:
                self._edit_end_ms = max(ms, self._edit_start_ms)
            self._emit_preview()
            self.update()
            return
        if self._editing is not None:
            hit = self._hit_for(self._editing)
            self._set_cursor("resize" if hit and self._handle_at(hit, pos) else "arrow")
        else:
            self._set_cursor("hand" if any(h.rect.contains(pos) for h in self._hits) else "arrow")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt API)
        if event.button() == Qt.LeftButton and self._drag_edge:
            self._drag_edge = None
            self.drag_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt API)
        self._set_cursor("arrow")
        super().leaveEvent(event)

    def _emit_preview(self) -> None:
        if self._editing is not None:
            self.edit_preview.emit(self._editing, int(self._edit_start_ms), int(self._edit_end_ms),
                                   self._drag_edge or "right")

    def _show_block_dialog(self, rec: AnnotationRecord) -> None:
        s_ms, e_ms = int(round(rec.start_time * 1000)), int(round(rec.end_time * 1000))
        box = QMessageBox(self)
        box.setWindowTitle("Annotation")
        box.setIcon(QMessageBox.Information)
        box.setText(
            f"Step {rec.step_no}: {rec.step_name}\n\n"
            f"Start: {ms_to_precise_str(s_ms)}  (frame {rec.start_frame})\n"
            f"End:   {ms_to_precise_str(e_ms)}  (frame {rec.end_frame})\n"
            f"Total: {rec.total_time:.3f} s\n\n"
            f"Time source: {rec.time_source}\nAudio source: {rec.audio_source}\n"
            f"Confidence: {int(rec.confidence)}"
        )
        box.addButton("Close", QMessageBox.RejectRole)
        btn_edit = box.addButton("Edit", QMessageBox.AcceptRole)
        btn_delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        btn_edit.setEnabled(self._allow_edit)
        btn_delete.setEnabled(self._allow_edit)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is btn_edit and self._allow_edit:
            self._enter_edit_mode(rec)
        elif clicked is btn_delete and self._allow_edit:
            self.request_delete.emit(rec)

    def _enter_edit_mode(self, rec: AnnotationRecord) -> None:
        if not self._contains(rec):
            return
        self._editing = self._selected = rec
        self._edit_start_ms = int(round(rec.start_time * 1000))
        self._edit_end_ms = int(round(rec.end_time * 1000))
        self._drag_edge = None
        self.request_edit.emit(rec)
        self._emit_preview()
        self.update()


class SkillTimeline(QScrollArea):
    """Scrollable timeline; forwards the canvas signals."""
    record_clicked = pyqtSignal(object)
    request_edit = pyqtSignal(object)
    request_delete = pyqtSignal(object)
    edit_preview = pyqtSignal(object, int, int, str)
    edit_committed = pyqtSignal(object, int, int)
    edit_canceled = pyqtSignal(object)
    drag_started = pyqtSignal()
    drag_finished = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # only as tall as its lanes (up to 4, then it scrolls): spare height goes to the videos
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.canvas = _TimelineCanvas(self)
        self.setWidget(self.canvas)
        for name in ("record_clicked", "request_edit", "request_delete", "edit_preview", "edit_committed",
                     "edit_canceled", "drag_started", "drag_finished"):
            getattr(self.canvas, name).connect(getattr(self, name).emit)

    def set_allow_edit(self, allow: bool) -> None:
        self.canvas.set_allow_edit(allow)

    def set_duration_ms(self, duration_ms: int) -> None:
        self.canvas.set_duration_ms(duration_ms)

    def set_color_resolver(self, fn: Callable[[int], str]) -> None:
        self.canvas.set_color_resolver(fn)

    def set_annotations(self, annotations: List[AnnotationRecord]) -> None:
        self.canvas.set_annotations(annotations)
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        lanes = min(4, max(1, len(self.canvas._lanes)))
        return QSize(400, self.canvas.height_for_lanes(lanes) + 2 * self.frameWidth())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(200, self.canvas.height_for_lanes(1) + 2 * self.frameWidth())

    def set_playhead_ms(self, ms: int) -> None:
        self.canvas.set_playhead_ms(ms)

    def selected_record(self) -> Optional[AnnotationRecord]:
        return self.canvas.selected_record()

    def set_selected(self, rec: Optional[AnnotationRecord]) -> None:
        self.canvas.set_selected(rec)

    def is_editing(self) -> bool:
        return self.canvas.editing_record() is not None

    def editing_record(self) -> Optional[AnnotationRecord]:
        return self.canvas.editing_record()

    def exit_editing(self, commit: bool = False) -> None:
        self.canvas.exit_editing(commit=commit)
