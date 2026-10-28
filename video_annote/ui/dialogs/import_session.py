# video_annote/ui/dialogs/import_session.py
"""Pick a session of the data root to open."""
from __future__ import annotations

import json
import os
import time
from typing import Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ...core.storage import (
    SESSION_META_FILENAME, TSV_FILENAME, list_sessions, session_dir, validate_importable_session,
)
from ..common import hand_cursor, hand_cursor_on_buttons, hand_cursor_on_items


def _session_facts(root_dir: str, label: str) -> Tuple[str, str, str]:
    """(videos, labels, last changed) of a session for the list; "?" where a file cannot be read."""
    folder = session_dir(root_dir, label)
    videos = labels = "?"
    try:
        with open(os.path.join(folder, SESSION_META_FILENAME), "rb") as f:
            entries = json.loads(f.read()).get("videos")
        videos = str(len(entries)) if isinstance(entries, list) else "?"
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(os.path.join(folder, TSV_FILENAME), "rb") as f:
            labels = str(max(0, sum(1 for line in f.read().splitlines() if line.strip()) - 1))
    except OSError:
        labels = "0" if not os.path.exists(os.path.join(folder, TSV_FILENAME)) else "?"
    stamps = [os.path.getmtime(p) for p in (os.path.join(folder, n) for n in (SESSION_META_FILENAME, TSV_FILENAME))
              if os.path.exists(p)]
    changed = time.strftime("%Y-%m-%d %H:%M", time.localtime(max(stamps))) if stamps else ""
    return videos, labels, changed


class ImportSessionDialog(QDialog):
    def __init__(self, root_dir: str, parent=None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Open Session")
        self.setModal(True)
        self.resize(640, 440)
        self._root_dir = root_dir
        self._current = current
        self._selected: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(QLabel("Sessions in the data root:"))
        top.addStretch()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        top.addWidget(self.filter)
        layout.addLayout(top)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Session", "Videos", "Labels", "Last changed"])
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setSortingEnabled(True)
        self.list.sortByColumn(0, Qt.AscendingOrder)
        self.list.itemDoubleClicked.connect(lambda _item, _col: self._on_accept())
        layout.addWidget(self.list, stretch=1)
        row = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._load_sessions)
        row.addWidget(self.btn_refresh)
        row.addStretch()
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        hand_cursor(self.btn_refresh)
        hand_cursor_on_buttons(buttons)
        hand_cursor_on_items(self.list)
        self._load_sessions()
        self.list.setFocus()

    def selected_label(self) -> Optional[str]:
        return self._selected

    def _load_sessions(self) -> None:
        self.list.clear()
        chosen = None
        for name in list_sessions(self._root_dir):
            item = QTreeWidgetItem([name, *_session_facts(self._root_dir, name)])
            item.setData(0, Qt.UserRole, name)
            for col in (1, 2):
                item.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
            self.list.addTopLevelItem(item)
            if name == self._current:
                chosen = item
        for col in range(1, 4):
            self.list.resizeColumnToContents(col)
        self.list.setColumnWidth(0, max(200, self.list.columnWidth(0)))
        self._apply_filter(self.filter.text())
        chosen = chosen or self.list.topLevelItem(0)  # the session opened last, else the first
        if chosen is not None:
            self.list.setCurrentItem(chosen)
            self.list.scrollToItem(chosen)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            item.setHidden(bool(needle) and needle not in item.text(0).lower())

    def _on_accept(self) -> None:
        item = self.list.currentItem()
        label = item.data(0, Qt.UserRole) if item is not None and not item.isHidden() else None
        if not label:
            QMessageBox.warning(self, "No selection", "Please select a session.")
            return
        ok, msg = validate_importable_session(self._root_dir, label)
        if not ok:
            QMessageBox.warning(self, "Cannot open session", msg)
            return
        self._selected = str(label)
        self.accept()
