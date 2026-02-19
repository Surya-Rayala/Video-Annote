# video_annote/dialogs/import_session.py
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..persistence import list_sessions, validate_importable_session


class ImportSessionDialog(QDialog):
    """
    Lists sessions in the given root dir and allows importing an existing session.
    """

    def __init__(self, root_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Existing Session")
        self.setModal(True)
        self.resize(620, 420)

        self._root_dir = root_dir
        self._selected_label: Optional[str] = None

        self._build_ui()
        self._load_sessions()
        self._apply_clickable_cursors()

    def selected_label(self) -> Optional[str]:
        return self._selected_label

    # ---------------- UI ----------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Select a session to import:"))

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.list, stretch=1)

        btn_row = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._load_sessions)
        btn_row.addWidget(self.btn_refresh)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_sessions(self):
        self.list.clear()
        if not self._root_dir:
            return
        sessions = list_sessions(self._root_dir)
        for s in sessions:
            it = QListWidgetItem(s)
            it.setData(Qt.UserRole, s)
            self.list.addItem(it)

        if sessions:
            self.list.setCurrentRow(0)

    # ---------------- Actions ----------------

    def _on_double_click(self, item: QListWidgetItem):
        if item is None:
            return
        self.list.setCurrentItem(item)
        self._on_accept()

    def _on_accept(self):
        item = self.list.currentItem()
        if not item:
            QMessageBox.warning(self, "No selection", "Please select a session.")
            return

        label = item.data(Qt.UserRole)
        if not label:
            QMessageBox.warning(self, "No selection", "Please select a session.")
            return

        ok, msg = validate_importable_session(self._root_dir, label)
        if not ok:
            QMessageBox.warning(self, "Cannot import session", msg)
            return

        self._selected_label = str(label)
        self.accept()
    def _apply_clickable_cursors(self) -> None:
        # Refresh button
        try:
            self.btn_refresh.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass

        # List rows are clickable/selectable; use a hand cursor over the viewport.
        try:
            self.list.setMouseTracking(True)
            self.list.viewport().setMouseTracking(True)
            self.list.viewport().installEventFilter(self)
        except Exception:
            pass

        # Dialog button box buttons
        try:
            for b in self.findChildren(QDialogButtonBox):
                for btn in b.buttons():
                    try:
                        btn.setCursor(Qt.PointingHandCursor)
                    except Exception:
                        pass
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # Cursor feedback for list viewport
        try:
            if hasattr(self, "list") and obj is self.list.viewport():
                et = event.type()
                if et == QEvent.MouseMove:
                    it = self.list.itemAt(event.pos())
                    self.list.viewport().setCursor(Qt.PointingHandCursor if it is not None else Qt.ArrowCursor)
                elif et in (QEvent.Leave, QEvent.HoverLeave):
                    self.list.viewport().setCursor(Qt.ArrowCursor)
        except Exception:
            pass
        return super().eventFilter(obj, event)