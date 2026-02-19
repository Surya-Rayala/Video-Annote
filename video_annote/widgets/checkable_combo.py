# video_annote/widgets/checkable_combo.py
from __future__ import annotations

from typing import List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import QComboBox


class CheckableComboBox(QComboBox):
    """
    A QComboBox that shows a checklist popup (multi-select) and displays a summary
    like "Selected (N) view(s)" when collapsed.

    Signals:
      - checked_ids_changed(list[str]): emitted when the checked set changes
      - empty_selection_attempted(): emitted when user attempts to uncheck the last item
      - dropdown_closed(): emitted when popup closes
    """
    checked_ids_changed = pyqtSignal(object)  # List[str]
    empty_selection_attempted = pyqtSignal()
    dropdown_closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # Cursor affordance (clickable)
        self.setCursor(Qt.PointingHandCursor)

        # Use the lineEdit area as a read-only display for the summary
        self.setEditable(True)
        if self.lineEdit():
            self.lineEdit().setReadOnly(True)
            self.lineEdit().setFocusPolicy(Qt.NoFocus)
            self.lineEdit().setCursor(Qt.PointingHandCursor)

        self._model = QStandardItemModel(self)
        self.setModel(self._model)

        # If Qt changes currentIndex internally, force our summary back
        try:
            self.currentIndexChanged.disconnect(self._on_current_index_changed)
        except Exception:
            pass
        self.currentIndexChanged.connect(self._on_current_index_changed)

        self._block = False
        self._refresh_display_text()

    # ---------------- Public API ----------------

    def set_items(self, ids: List[str]) -> None:
        self._block = True
        try:
            self._model.clear()
            for vid in (ids or []):
                it = QStandardItem(str(vid))
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                it.setData(Qt.Unchecked, Qt.CheckStateRole)
                self._model.appendRow(it)
        finally:
            self._block = False

        # Ensure single connection
        try:
            self._model.itemChanged.disconnect(self._on_item_changed)
        except Exception:
            pass
        self._model.itemChanged.connect(self._on_item_changed)

        # Keep currentIndex neutral so it won't show an item label
        self.setCurrentIndex(-1)

        self._refresh_display_text()

    def checked_ids(self) -> List[str]:
        out: List[str] = []
        for row in range(self._model.rowCount()):
            it = self._model.item(row)
            if it is None:
                continue
            if it.checkState() == Qt.Checked:
                out.append(it.text())
        return out

    def set_checked_ids(self, ids: List[str], emit_signal: bool = True) -> None:
        want = set(str(x) for x in (ids or []))
        self._block = True
        try:
            for row in range(self._model.rowCount()):
                it = self._model.item(row)
                if it is None:
                    continue
                it.setCheckState(Qt.Checked if it.text() in want else Qt.Unchecked)
        finally:
            self._block = False

        self.setCurrentIndex(-1)
        self._refresh_display_text()
        if emit_signal:
            self.checked_ids_changed.emit(self.checked_ids())

    # ---------------- Internal behavior ----------------

    def hidePopup(self) -> None:
        super().hidePopup()
        self.dropdown_closed.emit()

    def _on_current_index_changed(self, _idx: int):
        if self._block:
            return
        self.setCurrentIndex(-1)
        self._refresh_display_text()

    def _on_item_changed(self, item: QStandardItem) -> None:
        if self._block:
            return

        # Enforce "at least one checked"
        checked_now = self.checked_ids()
        if not checked_now:
            self._block = True
            try:
                item.setCheckState(Qt.Checked)
            finally:
                self._block = False
            self.setCurrentIndex(-1)
            self._refresh_display_text()
            self.empty_selection_attempted.emit()
            return

        self.setCurrentIndex(-1)
        self._refresh_display_text()
        self.checked_ids_changed.emit(checked_now)

    def _refresh_display_text(self) -> None:
        n = len(self.checked_ids())
        if n <= 0:
            txt = "Selected (0) videos"
        elif n == 1:
            txt = "Selected (1) video"
        else:
            txt = f"Selected ({n}) videos"

        # Prefer setEditText; fallback to lineEdit if needed
        try:
            self.setEditText(txt)
        except Exception:
            if self.lineEdit():
                self.lineEdit().setText(txt)