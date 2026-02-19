# video_annote/widgets/annotations_table.py
from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, QEvent, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..domain import AnnotationRecord, SkillStep
from ..timeutils import recompute_from_times


TABLE_COLUMNS = [
    "label", "camid", "step_no", "step_name",
    "start_frame", "end_frame", "total_frames",
    "start_time", "end_time", "total_time",
    "time_source", "audio_source", "confidence", "notes",
]


class AnnotationsTable(QTableWidget):
    """
    Bottom table for AnnotationRecord rows.

    Safe editing:
      - Editing is disabled by default
      - Right-click -> Edit cell enables editing for one cell only
      - Editable columns: step_no, start_time, end_time, confidence, notes
        (notes opens a larger dialog)
      - After edit, it updates underlying AnnotationRecord and emits annotations_changed

    Signals:
      - annotations_changed(list[AnnotationRecord])
      - request_note_edit(row_index)
    """
    annotations_changed = pyqtSignal(object)  # List[AnnotationRecord]
    request_scroll_to_row = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(0, len(TABLE_COLUMNS), parent)

        self.setHorizontalHeaderLabels(TABLE_COLUMNS)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setDefaultSectionSize(140)

        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)

        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self.itemChanged.connect(self._on_item_changed)
        self.itemDelegate().closeEditor.connect(self._on_close_editor)

        self._records: List[AnnotationRecord] = []
        self._skills: List[SkillStep] = []
        self._fps_provider = None  # callable(time_source_id:str)->fps:float

        self._updating = False
        self._editing_cell: Optional[Tuple[int, int]] = None
        self._editing_prev_text: Optional[str] = None
        self._handling_change = False

        # Hover affordance: table cells are clickable/selectable
        self.setMouseTracking(True)
        try:
            self.viewport().setMouseTracking(True)
            self.viewport().installEventFilter(self)
        except Exception:
            pass

        self._cursor_mode: str = "arrow"  # "arrow" | "hand"

    # ---------------- Public API ----------------

    def set_skills(self, skills: List[SkillStep]) -> None:
        self._skills = list(skills or [])

    def set_fps_provider(self, fn) -> None:
        """
        fn(time_source_id: str) -> fps float
        """
        self._fps_provider = fn

    def set_records(self, records: List[AnnotationRecord]) -> None:
        self._records = list(records or [])
        self.refresh()

    def records(self) -> List[AnnotationRecord]:
        return list(self._records)

    def selected_row_index(self) -> int:
        return self.currentRow()

    def delete_selected_record(self) -> None:
        row = self.currentRow()
        if row < 0 or row >= len(self._records):
            return

        rec = self._records[row]

        # Confirm delete (matches timeline behavior)
        resp = QMessageBox.question(
            self,
            "Delete annotation?",
            (
                "Delete this annotation?\n\n"
                f"Step {rec.step_no}: {rec.step_name}\n"
                f"Start: {rec.start_time:.3f}s   End: {rec.end_time:.3f}s\n\n"
                "This cannot be undone."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return

        del self._records[row]
        self.refresh()
        self.annotations_changed.emit(self.records())

    # ---------------- Rendering ----------------

    def refresh(self) -> None:
        self._updating = True
        try:
            self.setRowCount(0)
            for rec in self._records:
                row = self.rowCount()
                self.insertRow(row)

                # Notes: keep TSV-friendly display (show \n literal)
                notes_disp = (rec.notes or "").replace("\r", "").replace("\t", " ").replace("\n", "\\n")

                values = [
                    rec.label,
                    rec.camid,
                    str(rec.step_no),
                    rec.step_name,
                    str(rec.start_frame),
                    str(rec.end_frame),
                    str(rec.total_frames),
                    f"{rec.start_time:.3f}",
                    f"{rec.end_time:.3f}",
                    f"{rec.total_time:.3f}",
                    rec.time_source,
                    rec.audio_source,
                    str(int(rec.confidence)),
                    notes_disp,
                ]

                for col, val in enumerate(values):
                    item = QTableWidgetItem(val)
                    item.setToolTip(val)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.setItem(row, col, item)
        finally:
            self._updating = False

    # ---------------- Context menu editing ----------------

    def _show_context_menu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return
        row, col = item.row(), item.column()

        # Only allow safe columns
        # step_no(2), start_time(7), end_time(8), confidence(12), notes(13)
        allowed_cols = {2, 7, 8, 12, 13}

        menu = QMenu(self)
        edit_action = menu.addAction("Edit cell")
        if col not in allowed_cols:
            edit_action.setEnabled(False)

        delete_action = menu.addAction("Delete record")

        chosen = menu.exec_(self.viewport().mapToGlobal(pos))
        if chosen == delete_action:
            self.delete_selected_record()
            return

        if chosen == edit_action:
            if col not in allowed_cols:
                QMessageBox.information(
                    self,
                    "Editing disabled",
                    "Only step_no, start_time, end_time, confidence, and notes are editable here.\n"
                    "Other fields are derived and locked.",
                )
                return

            if col == 13:
                self._edit_notes_dialog(row)
                return

            self._begin_edit_cell(row, col)

    def _begin_edit_cell(self, row: int, col: int) -> None:
        item = self.item(row, col)
        if item is None:
            return

        self._lock_edit_cell()

        self._editing_cell = (row, col)
        self._editing_prev_text = item.text()

        # Enable editability for this item only
        self._updating = True
        try:
            item.setFlags(item.flags() | Qt.ItemIsEditable)
        finally:
            self._updating = False

        self.setCurrentCell(row, col)
        self.editItem(item)

    def _lock_edit_cell(self) -> None:
        if not self._editing_cell:
            return
        row, col = self._editing_cell
        item = self.item(row, col)
        if item is not None:
            self._updating = True
            try:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            finally:
                self._updating = False
        self._editing_cell = None
        self._editing_prev_text = None

    def _on_close_editor(self, editor, hint):
        self._lock_edit_cell()

    # ---------------- Notes dialog ----------------

    def _edit_notes_dialog(self, row: int) -> None:
        if row < 0 or row >= len(self._records):
            return
        rec = self._records[row]

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Notes")
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)

        title = QLabel(f"Step {rec.step_no}: {rec.step_name}")
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(title)

        layout.addWidget(QLabel("Notes:"))
        notes_edit = QTextEdit()
        notes_edit.setPlainText(rec.notes or "")
        notes_edit.setPlaceholderText("Add any additional context or explanation here...")
        notes_edit.setMinimumSize(520, 220)
        layout.addWidget(notes_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec_() == QDialog.Accepted:
            rec.notes = notes_edit.toPlainText().strip()
            self.refresh()
            self.annotations_changed.emit(self.records())
            self.request_scroll_to_row.emit(row)

    # ---------------- Apply edits ----------------

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._updating or self._handling_change or item is None:
            return

        row, col = item.row(), item.column()
        if not self._editing_cell or self._editing_cell != (row, col):
            return

        new_text = item.text().strip()
        prev_text = (self._editing_prev_text or "").strip()

        if new_text == prev_text:
            self._lock_edit_cell()
            return

        self._handling_change = True
        try:
            try:
                self._apply_edit_to_record(row, col, new_text)
            except ValueError as e:
                QMessageBox.warning(self, "Invalid value", str(e))
                # revert
                prev = self._editing_prev_text if self._editing_prev_text is not None else ""
                self._updating = True
                try:
                    item.setText(prev)
                finally:
                    self._updating = False
                self._lock_edit_cell()
                return

            self._lock_edit_cell()
            self.refresh()
            self.annotations_changed.emit(self.records())
            self.request_scroll_to_row.emit(row)
        finally:
            self._handling_change = False

    def _fps_for_time_source(self, time_source_id: str) -> float:
        if callable(self._fps_provider):
            try:
                fps = float(self._fps_provider(time_source_id))
                if fps > 0:
                    return fps
            except Exception:
                pass
        return 30.0

    def _apply_edit_to_record(self, row: int, col: int, text: str) -> None:
        if row < 0 or row >= len(self._records):
            return
        rec = self._records[row]

        def as_int(v: str, field: str) -> int:
            try:
                return int(v)
            except Exception:
                raise ValueError(f"{field} must be an integer")

        def as_float(v: str, field: str) -> float:
            try:
                return float(v)
            except Exception:
                raise ValueError(f"{field} must be a number")

        if col == 2:
            new_step_no = as_int(text, "step_no")
            if new_step_no <= 0:
                raise ValueError("step_no must be a positive integer")

            matching = next((s for s in self._skills if s.number == new_step_no), None)
            if matching is None:
                raise ValueError(
                    f"Step number {new_step_no} is not defined in the Skills list. "
                    f"Please add it first (right panel) or choose an existing step number."
                )

            rec.step_no = new_step_no
            rec.step_name = matching.name
            # Totals remain consistent
            rec.total_time = max(0.0, float(rec.end_time) - float(rec.start_time))
            rec.total_frames = max(0, int(rec.end_frame) - int(rec.start_frame))

        elif col == 7:
            st = as_float(text, "start_time")
            rec.start_time = st
            fps = self._fps_for_time_source(rec.time_source)
            new_rec = recompute_from_times(rec, fps)
            rec.start_time = new_rec.start_time
            rec.end_time = new_rec.end_time
            rec.total_time = new_rec.total_time
            rec.start_frame = new_rec.start_frame
            rec.end_frame = new_rec.end_frame
            rec.total_frames = new_rec.total_frames

        elif col == 8:
            et = as_float(text, "end_time")
            rec.end_time = et
            fps = self._fps_for_time_source(rec.time_source)
            new_rec = recompute_from_times(rec, fps)
            rec.start_time = new_rec.start_time
            rec.end_time = new_rec.end_time
            rec.total_time = new_rec.total_time
            rec.start_frame = new_rec.start_frame
            rec.end_frame = new_rec.end_frame
            rec.total_frames = new_rec.total_frames

        elif col == 12:
            new_conf = as_int(text, "confidence")
            if not (1 <= new_conf <= 10):
                raise ValueError("confidence must be between 1 and 10")
            rec.confidence = new_conf

        elif col == 13:
            # usually edited via dialog, but keep safe
            rec.notes = text

        else:
            raise ValueError("This field is not editable.")
    def _set_cursor_mode(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode == self._cursor_mode:
            return
        self._cursor_mode = mode
        try:
            if mode == "hand":
                self.viewport().setCursor(Qt.PointingHandCursor)
            else:
                self.viewport().setCursor(Qt.ArrowCursor)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # Cursor feedback for table viewport (hover rows/cells)
        try:
            if obj is self.viewport():
                et = event.type()
                if et == QEvent.MouseMove:
                    it = self.itemAt(event.pos())
                    if it is not None:
                        self._set_cursor_mode("hand")
                    else:
                        self._set_cursor_mode("arrow")
                elif et in (QEvent.Leave, QEvent.HoverLeave):
                    self._set_cursor_mode("arrow")
        except Exception:
            pass
        return super().eventFilter(obj, event)