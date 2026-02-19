# video_annote/dialogs/create_session.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..media_import import (
    ImportedVideoInfo,
    is_probably_url,
    validate_local_video_path,
    validate_video_url,
    next_video_id,
)


@dataclass
class PendingVideo:
    """
    A video the user wants to add to the session before committing.

    type: "local" or "url"
    value: local path or url
    """
    video_id: str
    source_type: str
    source_value: str


class CreateSessionDialog(QDialog):
    """
    Collects:
      - session label
      - list of videos (Video-1..Video-N) with either local path or URL

    Does NOT write files itself. It returns a structured list that the main window commits
    (copy/remux into session folder, save session meta, etc).

    Behavior:
      - Add Video -> choose Local or URL
      - Strict validation at add time
      - List is shown in a scrollable list
      - No edits after OK in main UI; this dialog is the only place to stage changes
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create New Session")
        self.setModal(True)
        self.resize(720, 420)

        self._videos: List[PendingVideo] = []

        self._build_ui()
        self._apply_clickable_cursors()

    # ---------------- UI ----------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Label input
        label_row = QHBoxLayout()
        label_row.addWidget(QLabel("Session Label:"))
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g., session_001")
        self.label_edit.setCursor(Qt.IBeamCursor)
        label_row.addWidget(self.label_edit, stretch=1)
        layout.addLayout(label_row)

        # Video list + buttons
        layout.addWidget(QLabel("Videos (Video-1 … Video-N):"))

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.list, stretch=1)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("Add Video")
        self.btn_add.clicked.connect(self._on_add_video)
        self.btn_remove = QPushButton("Remove Selected")
        self.btn_remove.clicked.connect(self._on_remove_selected)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_clickable_cursors(self) -> None:
        # Buttons
        try:
            self.btn_add.setCursor(Qt.PointingHandCursor)
            self.btn_remove.setCursor(Qt.PointingHandCursor)
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
                if et in (QEvent.MouseMove, QEvent.HoverMove):
                    it = self.list.itemAt(event.pos())
                    self.list.viewport().setCursor(Qt.PointingHandCursor if it is not None else Qt.ArrowCursor)
                elif et in (QEvent.Leave, QEvent.HoverLeave):
                    self.list.viewport().setCursor(Qt.ArrowCursor)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ---------------- Public API ----------------

    def session_label(self) -> str:
        return self.label_edit.text().strip()

    def pending_videos(self) -> List[PendingVideo]:
        return list(self._videos)

    # ---------------- Actions ----------------

    def _on_add_video(self):
        # Prompt choice via menu anchored to button
        menu = QMenu(self)
        act_local = menu.addAction("Local file…")
        act_url = menu.addAction("URL… (m3u8 / downloadable video)")
        chosen = menu.exec_(self.btn_add.mapToGlobal(self.btn_add.rect().bottomLeft()))
        if chosen == act_local:
            self._add_local()
        elif chosen == act_url:
            self._add_url()

    def _add_local(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video File",
            "",
            "Video Files (*.mp4 *.mov *.mkv *.avi *.m4v *.webm);;All Files (*)",
        )
        if not path:
            return
        ok, msg = validate_local_video_path(path)
        if not ok:
            QMessageBox.warning(self, "Invalid file", msg)
            return

        vid = next_video_id(len(self._videos))
        self._videos.append(PendingVideo(video_id=vid, source_type="local", source_value=path))
        self._refresh_list()

    def _add_url(self):
        # Simple URL prompt
        url, ok = self._prompt_text("Enter video URL", "URL (m3u8 or downloadable video):")
        if not ok:
            return
        url = (url or "").strip()
        ok2, msg = validate_video_url(url)
        if not ok2:
            QMessageBox.warning(self, "Invalid URL", msg)
            return

        vid = next_video_id(len(self._videos))
        self._videos.append(PendingVideo(video_id=vid, source_type="url", source_value=url))
        self._refresh_list()

    def _on_remove_selected(self):
        row = self.list.currentRow()
        if row < 0 or row >= len(self._videos):
            return
        del self._videos[row]
        # Re-number video ids to stay sequential (video-1..N)
        self._videos = [
            PendingVideo(video_id=f"video-{i+1}", source_type=v.source_type, source_value=v.source_value)
            for i, v in enumerate(self._videos)
        ]
        self._refresh_list()

    def _on_context_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item:
            return
        row = item.data(Qt.UserRole)
        if row is None:
            return

        menu = QMenu(self)
        act_remove = menu.addAction("Remove")
        chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
        if chosen == act_remove:
            self.list.setCurrentRow(int(row))
            self._on_remove_selected()

    def _on_accept(self):
        label = self.session_label()
        if not label:
            QMessageBox.warning(self, "Missing label", "Please enter a session label.")
            return
        if not self._videos:
            QMessageBox.warning(self, "No videos", "Please add at least one video.")
            return

        # Basic label safety: disallow path separators
        if any(x in label for x in ("/", "\\", ":", "..")):
            QMessageBox.warning(self, "Invalid label", "Session label contains invalid characters.")
            return

        self.accept()

    # ---------------- Helpers ----------------

    def _refresh_list(self):
        self.list.clear()
        for idx, v in enumerate(self._videos):
            src = v.source_value
            src_disp = src
            if len(src_disp) > 80:
                src_disp = src_disp[:40] + " … " + src_disp[-35:]
            text = f"{v.video_id}  |  {v.source_type.upper()}  |  {src_disp}"
            it = QListWidgetItem(text)
            it.setToolTip(src)
            it.setData(Qt.UserRole, idx)
            self.list.addItem(it)

    def _prompt_text(self, title: str, label: str) -> Tuple[str, bool]:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.resize(600, 120)

        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(label))

        edit = QLineEdit()
        edit.setPlaceholderText("https://…")
        edit.setCursor(Qt.IBeamCursor)
        layout.addWidget(edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        try:
            for btn in buttons.buttons():
                btn.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass

        ok = dlg.exec_() == QDialog.Accepted
        return (edit.text(), ok)