# video_annote/ui/widgets/checkable_combo.py
from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtGui import QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import QComboBox


class CheckableComboBox(QComboBox):
    """
    A QComboBox that shows a checklist popup (multi-select) and displays a summary
    like "2 of 3 shown" when collapsed.

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
        # a click anywhere on a row toggles it and keeps the list open (not only on the small checkbox)
        self.view().viewport().installEventFilter(self)

        # If Qt changes currentIndex internally, force our summary back
        try:
            self.currentIndexChanged.disconnect(self._on_current_index_changed)
        except Exception:
            pass
        self.currentIndexChanged.connect(self._on_current_index_changed)

        self._block = False
        self._refresh_display_text()

    # ---------------- Public API ----------------

    def set_items(self, ids: List[str], labels: Optional[List[str]] = None) -> None:
        """Items ``ids`` (what the signals report), shown as ``labels`` (default: the ids)."""
        self._block = True
        try:
            self._model.clear()
            for i, vid in enumerate(ids or []):
                it = QStandardItem(str(labels[i] if labels and i < len(labels) else vid))
                it.setData(str(vid), Qt.UserRole)
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
                out.append(str(it.data(Qt.UserRole)))
        return out

    def set_checked_ids(self, ids: List[str], emit_signal: bool = True) -> None:
        want = set(str(x) for x in (ids or []))
        self._block = True
        try:
            for row in range(self._model.rowCount()):
                it = self._model.item(row)
                if it is None:
                    continue
                it.setCheckState(Qt.Checked if str(it.data(Qt.UserRole)) in want else Qt.Unchecked)
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

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if obj is self.view().viewport() and event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                                                              QEvent.MouseButtonDblClick):
            index = self.view().indexAt(event.pos())
            if index.isValid() and event.type() == QEvent.MouseButtonRelease:
                item = self._model.itemFromIndex(index)
                item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
            return index.isValid()  # handled here: the popup stays open for more choices
        return super().eventFilter(obj, event)

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
        n, total = len(self.checked_ids()), self._model.rowCount()
        txt = f"{n} of {total} shown" if total else "No videos"
        try:
            self.setEditText(txt)
        except Exception:
            if self.lineEdit():
                self.lineEdit().setText(txt)
        if self.lineEdit():
            self.lineEdit().setCursorPosition(0)  # the start of the text when it is wider than the box
