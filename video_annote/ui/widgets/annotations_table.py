# video_annote/ui/widgets/annotations_table.py
"""Table view of the annotations with guarded in-place editing.

The table never mutates a record. An edit is parsed into a modified *copy* and handed to the main
window (``edit_requested(old, new)``), which validates, snaps to frames, saves and refreshes. Rows
are tied to record objects, not positions."""
from __future__ import annotations

import dataclasses
import math
from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QMenu, QMessageBox, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from ...core.models import AnnotationRecord, SkillStep
from ...core.timecode import ms_to_precise_str, parse_time
from ...core.storage import TSV_HEADER, escape_notes
from ..common import app_settings, hand_cursor_on_items

EDITABLE = {"step_no", "start_time", "end_time", "confidence", "notes"}
# column titles for people; each title's tooltip names the label.tsv column
TITLES = {"label": "Session", "camid": "Views", "step_no": "Step", "step_name": "Name", "start_frame": "Start frame",
          "end_frame": "End frame", "total_frames": "Frames", "start_time": "Start", "end_time": "End",
          "total_time": "Length", "time_source": "Time source", "audio_source": "Audio source",
          "confidence": "Confidence", "notes": "Notes"}
# shown unless chosen otherwise (right-click the column titles); the session's label is the same on every row
SHOWN = ("step_no", "step_name", "start_time", "end_time", "total_time", "confidence", "notes")


def _clock(seconds: float) -> str:
    return ms_to_precise_str(int(round(float(seconds) * 1000.0)))


class AnnotationsTable(QTableWidget):
    edit_requested = pyqtSignal(object, object)  # old record, edited copy
    delete_requested = pyqtSignal(object)  # record
    record_selected = pyqtSignal(object)  # record or None: the selection changed
    record_activated = pyqtSignal(object)  # record: double-clicked
    go_to_requested = pyqtSignal(object, str)  # record, "start" | "end": show that frame
    set_edge_requested = pyqtSignal(object, str)  # record, "start" | "end": set it to the frame on screen

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(0, len(TSV_HEADER), parent)
        self.setHorizontalHeaderLabels([TITLES[c] for c in TSV_HEADER])
        for col, name in enumerate(TSV_HEADER):
            self.horizontalHeaderItem(col).setToolTip(f"label.tsv column: {name} · right-click to show other columns")
        hidden = app_settings().value("table/hidden")  # (the choice made last time: names, comma-separated)
        hidden = set(filter(None, hidden.split(","))) if isinstance(hidden, str) else \
            {c for c in TSV_HEADER if c not in SHOWN}
        for col, name in enumerate(TSV_HEADER):
            self.setColumnHidden(col, name == "label" or name in hidden)
        self.horizontalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
        self.horizontalHeader().customContextMenuRequested.connect(self._choose_columns)
        self.horizontalHeader().setStretchLastSection(True)
        clock = self.fontMetrics().horizontalAdvance("00:00.000") + 18  # (times read 01:04.809)
        for col, name in enumerate(TSV_HEADER):  # every title and value readable; names get more room
            width = self.fontMetrics().horizontalAdvance(TITLES[name]) + 18
            least = {"step_name": 200, "camid": 120, "start_time": clock, "end_time": clock, "total_time": clock}
            self.setColumnWidth(col, max(width, least.get(name, 56)))
        self.setWordWrap(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setTabKeyNavigation(False)  # Tab moves on to the buttons below instead of cell by cell
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.itemChanged.connect(self._on_item_changed)
        self.itemDelegate().closeEditor.connect(lambda *_: self._lock_edit_cell())
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.cellDoubleClicked.connect(self._on_double_click)
        hand_cursor_on_items(self)

        self._records: List[AnnotationRecord] = []
        self._skills: List[SkillStep] = []
        self._updating = False
        self._editing_cell: Optional[Tuple[int, int]] = None
        self._editing_prev_text = ""

    # ---------------- public API ----------------

    def set_skills(self, skills: List[SkillStep]) -> None:
        self._skills = list(skills or [])

    def set_records(self, records: List[AnnotationRecord]) -> None:
        self._records = list(records or [])
        self.refresh()

    def selected_record(self) -> Optional[AnnotationRecord]:
        rows = self.selectionModel().selectedRows()
        row = rows[0].row() if rows else -1
        return self._records[row] if 0 <= row < len(self._records) else None

    def select_record(self, rec: AnnotationRecord) -> None:
        for row, r in enumerate(self._records):
            if r is rec:
                self.selectRow(row)
                self.scrollToItem(self.item(row, 0))
                return

    def request_delete_selected(self) -> None:
        rec = self.selected_record()
        if rec is not None:
            self.delete_requested.emit(rec)

    def _on_selection_changed(self) -> None:
        if not self._updating:
            rows = {i.row() for i in self.selectedItems()}
            self.record_selected.emit(self._records[min(rows)] if rows and min(rows) < len(self._records) else None)

    def _on_double_click(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._records):
            self.record_activated.emit(self._records[row])

    # ---------------- rendering ----------------

    def refresh(self) -> None:
        selected = self.selected_record()  # kept selected across the rebuild, if it is still there
        self._updating = True
        try:
            self.setRowCount(0)
            for rec in self._records:
                row = self.rowCount()
                self.insertRow(row)
                values = [
                    rec.label, rec.camid, str(rec.step_no), rec.step_name,
                    str(rec.start_frame), str(rec.end_frame), str(rec.total_frames),
                    _clock(rec.start_time), _clock(rec.end_time), _clock(rec.total_time),
                    rec.time_source, rec.audio_source, str(int(rec.confidence)), escape_notes(rec.notes),
                ]
                seconds = {7: rec.start_time, 8: rec.end_time, 9: rec.total_time}  # as label.tsv stores them
                for col, val in enumerate(values):
                    item = QTableWidgetItem(val)
                    item.setToolTip(f"{val}  ({seconds[col]:.3f} s)" if col in seconds else val)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.setItem(row, col, item)
        finally:
            self._updating = False
        if selected is not None and any(r is selected for r in self._records):
            self.select_record(selected)
        elif selected is not None:
            self.record_selected.emit(None)  # it is gone (deleted): nothing is selected now

    def _choose_columns(self, pos) -> None:
        """Right-click on the column titles: show or hide columns (remembered)."""
        menu = QMenu(self)
        for col, name in enumerate(TSV_HEADER):
            if name == "label":
                continue
            action = menu.addAction(TITLES[name])
            action.setCheckable(True)
            action.setChecked(not self.isColumnHidden(col))
            action.toggled.connect(lambda on, c=col: self.setColumnHidden(c, not on))
        menu.exec_(self.horizontalHeader().viewport().mapToGlobal(pos))
        app_settings().setValue("table/hidden", ",".join(n for c, n in enumerate(TSV_HEADER)
                                                     if n != "label" and self.isColumnHidden(c)))

    # ---------------- editing ----------------

    def _show_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        row, col = item.row(), item.column()
        self.selectRow(row)
        menu = QMenu(self)
        rec = self._records[row] if 0 <= row < len(self._records) else None
        go_start = menu.addAction("Show First Frame\t[")
        go_end = menu.addAction("Show Last Frame\t]")
        menu.addSeparator()
        set_start = menu.addAction("Set Start to the Frame on Screen\tShift+I")
        set_end = menu.addAction("Set End to the Frame on Screen\tShift+O")
        edit_action = menu.addAction("Edit Cell…")
        edit_action.setEnabled(TSV_HEADER[col] in EDITABLE)
        menu.addSeparator()
        delete_action = menu.addAction("Delete Annotation…")
        chosen = menu.exec_(self.viewport().mapToGlobal(pos))
        if rec is None or chosen is None:
            return
        if chosen in (go_start, go_end):
            self.go_to_requested.emit(rec, "start" if chosen is go_start else "end")
        elif chosen in (set_start, set_end):
            self.set_edge_requested.emit(rec, "start" if chosen is set_start else "end")
        elif chosen is delete_action:
            self.request_delete_selected()
        elif chosen is edit_action:
            if TSV_HEADER[col] == "notes":
                self._edit_notes_dialog(row)
            else:
                self._begin_edit_cell(row, col)

    def _begin_edit_cell(self, row: int, col: int) -> None:
        item = self.item(row, col)
        if item is None:
            return
        self._lock_edit_cell()
        self._editing_cell = (row, col)
        self._editing_prev_text = item.text()
        self._updating = True
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self._updating = False
        self.setCurrentCell(row, col)
        self.editItem(item)

    def _lock_edit_cell(self) -> None:
        if self._editing_cell is None:
            return
        item = self.item(*self._editing_cell)
        if item is not None:
            self._updating = True
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._updating = False
        self._editing_cell = None

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item is None or self._editing_cell != (item.row(), item.column()):
            return
        row, col = item.row(), item.column()
        text = item.text().strip()
        self._lock_edit_cell()
        if text == self._editing_prev_text.strip() or not 0 <= row < len(self._records):
            return
        old = self._records[row]
        try:
            new = self._edited_copy(old, TSV_HEADER[col], text)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid value", str(e))
            self.refresh()
            return
        self.edit_requested.emit(old, new)

    def _edited_copy(self, rec: AnnotationRecord, field: str, text: str) -> AnnotationRecord:
        if field == "step_no":
            try:
                number = int(text)
            except ValueError:
                raise ValueError("step_no must be a whole number") from None
            step = next((s for s in self._skills if s.number == number), None)
            if step is None:
                raise ValueError(f"Label number {number} is not in the Labels list. Add it first.")
            return dataclasses.replace(rec, step_no=number, step_name=step.name)
        if field in ("start_time", "end_time"):
            try:
                value = parse_time(text)
            except ValueError:
                raise ValueError(f"{TITLES[field]} must be seconds (64.5) or minutes:seconds (01:04.500)") from None
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field} must be a finite, non-negative number of seconds")
            return dataclasses.replace(rec, **{field: value})
        if field == "confidence":
            try:
                value = int(text)
            except ValueError:
                raise ValueError("confidence must be a whole number") from None
            if not 1 <= value <= 10:
                raise ValueError("confidence must be between 1 and 10")
            return dataclasses.replace(rec, confidence=value)
        if field == "notes":
            return dataclasses.replace(rec, notes=text)
        raise ValueError(f"{field} is derived and cannot be edited")

    def _edit_notes_dialog(self, row: int) -> None:
        if not 0 <= row < len(self._records):
            return
        rec = self._records[row]
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Notes")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"Step {rec.step_no}: {rec.step_name}"))
        edit = QTextEdit()
        edit.setPlainText(rec.notes or "")
        edit.setMinimumSize(520, 220)
        layout.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec_() == QDialog.Accepted:
            notes = edit.toPlainText().strip()
            if notes != (rec.notes or ""):
                self.edit_requested.emit(rec, dataclasses.replace(rec, notes=notes))
