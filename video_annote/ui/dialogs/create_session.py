# video_annote/ui/dialogs/create_session.py
"""Collects a session label and an ordered list of videos (local files or URLs), each with an
optional recording start typed by hand. Writes nothing: the main window imports the videos in the
background once the dialog is accepted."""
from __future__ import annotations

import html
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QVBoxLayout,
)

from ...core.storage import validate_session_label
from ...media.ffmpeg import FFmpegError
from ...media.probe import probe
from ...media.recorded import EnteredTime, Stamp, find_recording_time, read_entered
from ...sessions import ALLOWED_VIDEO_EXTS, SourceSpec, validate_local_video_path, validate_video_url
from ..common import hand_cursor, hand_cursor_on_buttons, hand_cursor_on_items
from ..widgets.start_time import StartTimeEdit, readable_time


@dataclass
class _Video:
    kind: str  # "local" | "url"
    value: str  # path or URL
    file_start: Optional[Stamp] = None  # when the file says it was recorded (local files)
    entered: EnteredTime = EnteredTime("")  # the recording start typed for it, confirmed


def _file_start(path: str) -> Optional[Stamp]:
    try:
        return find_recording_time(path, probe(path, timeout=30))
    except (FFmpegError, OSError):
        return None


class CreateSessionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Session")
        self.setModal(True)
        self.resize(760, 560)
        self.setAcceptDrops(True)  # video files dragged from Finder / Explorer are added
        self._videos: List[_Video] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        row = QHBoxLayout()
        row.addWidget(QLabel("Session label:"))
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g., session_001")
        self.label_edit.textChanged.connect(lambda _t: self._update_import_button())
        row.addWidget(self.label_edit, stretch=1)
        layout.addLayout(row)

        layout.addWidget(QLabel("Videos in camera order (the first becomes video-1). Add them with Add Video, or "
                                "drag files here:"))
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        self.list.currentRowChanged.connect(lambda _r: self._show_selected())
        layout.addWidget(self.list, stretch=1)

        buttons_row = QHBoxLayout()
        self.btn_add = QPushButton("Add Video")
        self.btn_add.clicked.connect(self._on_add_video)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._on_remove_selected)
        self.btn_up = QPushButton("Move Up")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = QPushButton("Move Down")
        self.btn_down.clicked.connect(lambda: self._move(1))
        buttons_row.addWidget(self.btn_add)
        buttons_row.addWidget(self.btn_remove)
        buttons_row.addSpacing(12)
        buttons_row.addWidget(self.btn_up)
        buttons_row.addWidget(self.btn_down)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)

        # the recording start of the selected video (optional)
        self.start_box = QGroupBox("Recording start (optional)")
        start_layout = QVBoxLayout(self.start_box)
        self.start_intro = QLabel("")
        self.start_intro.setWordWrap(True)
        self.start_intro.setStyleSheet("color: #888;")
        start_layout.addWidget(self.start_intro)
        self.start_edit = StartTimeEdit()
        self.btn_confirm = QPushButton("Confirm")
        self.btn_confirm.setToolTip("Use this time as the recording start of the selected video")
        self.btn_confirm.clicked.connect(self._confirm_start)
        self.start_edit.row.addWidget(self.btn_confirm)
        self.start_edit.changed.connect(self._update_confirm)
        self.start_edit.confirmed.connect(self._confirm_start)
        start_layout.addWidget(self.start_edit)
        layout.addWidget(self.start_box)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Import")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        hand_cursor(self.btn_add, self.btn_remove, self.btn_up, self.btn_down, self.btn_confirm)
        hand_cursor_on_buttons(self.buttons)
        hand_cursor_on_items(self.list)
        self._refresh_list()

    # ---------------- results ----------------

    def session_label(self) -> str:
        return self.label_edit.text().strip()

    def sources(self) -> List[SourceSpec]:
        return [SourceSpec(f"video-{i + 1}", v.value, v.kind, v.entered) for i, v in enumerate(self._videos)]

    # ---------------- adding and ordering ----------------

    def _on_add_video(self) -> None:
        menu = QMenu(self)
        act_local = menu.addAction("Local file…")
        act_url = menu.addAction("URL… (m3u8 / downloadable video)")
        chosen = menu.exec_(self.btn_add.mapToGlobal(self.btn_add.rect().bottomLeft()))
        if chosen is act_local:
            self._add_local()
        elif chosen is act_url:
            self._add_url()

    def _add_local(self) -> None:
        patterns = " ".join(f"*{e}" for e in sorted(ALLOWED_VIDEO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(self, "Select Video Files", "",
                                                f"Video Files ({patterns});;All Files (*)")
        self._add_paths(paths)

    def _add_paths(self, paths: List[str]) -> None:
        refused = []
        QApplication.setOverrideCursor(Qt.WaitCursor)  # (each file is read for its recording time)
        try:
            for path in paths:
                ok, msg = validate_local_video_path(path)
                if ok:
                    self._videos.append(_Video("local", path, _file_start(path)))
                else:
                    refused.append(msg)
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_list(select=len(self._videos) - 1)
        if refused:
            QMessageBox.warning(self, "Not added", "\n".join(refused[:10]))

    def _add_url(self) -> None:
        url, ok = self._prompt_text("Enter video URL", "URL (m3u8 or downloadable video):")
        url = (url or "").strip()
        if not ok or not url:
            return
        valid, msg = validate_video_url(url)
        if not valid:
            QMessageBox.warning(self, "Invalid URL", msg)
            return
        self._videos.append(_Video("url", url))
        self._refresh_list(select=len(self._videos) - 1)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._add_paths([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
        event.acceptProposedAction()

    def _move(self, step: int) -> None:
        row, target = self.list.currentRow(), self.list.currentRow() + step
        if 0 <= row < len(self._videos) and 0 <= target < len(self._videos):
            self._videos[row], self._videos[target] = self._videos[target], self._videos[row]
            self._refresh_list(select=target)

    def _on_remove_selected(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._videos):
            del self._videos[row]
            self._refresh_list(select=min(row, len(self._videos) - 1))

    def _on_context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        act_remove = menu.addAction("Remove")
        if menu.exec_(self.list.viewport().mapToGlobal(pos)) is act_remove:
            self.list.setCurrentItem(item)
            self._on_remove_selected()

    # ---------------- recording start ----------------

    def _selected(self) -> Tuple[int, Optional[_Video]]:
        row = self.list.currentRow()
        return row, (self._videos[row] if 0 <= row < len(self._videos) else None)

    @staticmethod
    def _style_row(label: QLabel, selected: bool) -> None:
        """Two lines; the second quieter, but still readable on the selection's highlight."""
        first, second = label.property("lines")
        label.setText(f"{first}<br><span style='color:{'#e8e8e8' if selected else '#888'}'>{second}</span>")

    def _show_selected(self) -> None:
        for i in range(self.list.count()):
            label = self.list.itemWidget(self.list.item(i))
            if label is not None:
                self._style_row(label, selected=i == self.list.currentRow())
        row, video = self._selected()
        self.start_box.setVisible(video is not None)
        if video is None:
            return
        self.start_box.setTitle(f"Recording start of video-{row + 1} (optional)")
        if video.file_start is not None:
            said = f"The file says {readable_time(video.file_start.when, video.file_start.precision_s)}."
        else:
            said = ("The file does not say when it was recorded." if video.kind == "local"
                    else "A stream's own time, if it states one, is read during the import.")
        self.start_intro.setText(f"{said} If you know it, enter the recording start; it is used instead.")
        if video.entered.text:
            self.start_edit.set_entered(video.entered)
        else:
            self.start_edit.reset()
        self._update_confirm()

    def _update_confirm(self) -> None:
        _row, video = self._selected()
        typed = self.start_edit.entered()
        if video is not None and not typed.text and video.entered.text:
            self.btn_confirm.setText("Remove")
            self.btn_confirm.setEnabled(True)
        else:
            self.btn_confirm.setText("Confirm")
            self.btn_confirm.setEnabled(video is not None and self.start_edit.value() is not None
                                        and typed != video.entered)

    def _confirm_start(self) -> None:
        row, video = self._selected()
        if video is None or (self.start_edit.text() and self.start_edit.value() is None):
            return
        video.entered = self.start_edit.entered() if self.start_edit.text() else EnteredTime("")
        self._refresh_list(select=row)

    def _unconfirmed(self) -> bool:
        """True to go on with the import, after asking about a start time typed but not confirmed."""
        row, video = self._selected()
        typed = self.start_edit.entered()
        if video is None or typed == video.entered or not typed.text:
            return True
        t = self.start_edit.value()
        if t is None:
            QMessageBox.warning(self, "Recording start", f"The recording start typed for video-{row + 1} cannot be "
                                                         "read. Correct it, or clear it to import without it.")
            return False
        answer = QMessageBox.question(
            self, "Recording start", f"Use the recording start you typed for video-{row + 1}?\n\n"
            f"{readable_time(t.when, t.precision_s)}", QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Yes:
            video.entered = typed
        return True

    # ---------------- import ----------------

    def _on_accept(self) -> None:
        error = validate_session_label(self.session_label())
        if error:
            QMessageBox.warning(self, "Invalid label", error)
            return
        if not self._videos:
            QMessageBox.warning(self, "No videos", "Please add at least one video.")
            return
        if self._unconfirmed():
            self.accept()

    def _update_import_button(self) -> None:
        """Import is available once there is a valid label and at least one video (Return in the label field
        then imports; it never pops up a complaint)."""
        problem = validate_session_label(self.session_label()) or ("" if self._videos else "Add at least one video.")
        button = self.buttons.button(QDialogButtonBox.Ok)
        button.setEnabled(not problem)
        button.setToolTip(problem)

    # ---------------- helpers ----------------

    def _refresh_list(self, select: int = -1) -> None:
        self._update_import_button()
        self.list.clear()
        for i, video in enumerate(self._videos):
            shown = video.value if video.kind == "url" else os.path.basename(video.value)
            if video.entered.text:
                t = read_entered(video.entered)
                start = f"Starts {readable_time(t.when, t.precision_s)} · entered"
            elif video.file_start is not None:
                start = f"Starts {readable_time(video.file_start.when, video.file_start.precision_s)} · from the file"
            else:
                start = ("Recording start not in the file" if video.kind == "local"
                         else "Recording start: read during the import, if the stream states it")
            label = QLabel()
            label.setProperty("lines", (f"<b>video-{i + 1}</b> &nbsp; {html.escape(shown)}", html.escape(start)))
            label.setContentsMargins(6, 3, 6, 3)
            self._style_row(label, selected=False)
            item = QListWidgetItem()
            item.setToolTip(video.value)
            item.setSizeHint(label.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, label)
        if 0 <= select < len(self._videos):
            self.list.setCurrentRow(select)
        self._show_selected()

    def _prompt_text(self, title: str, label: str) -> Tuple[Optional[str], bool]:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(600, 120)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(label))
        edit = QLineEdit()
        edit.setPlaceholderText("https://…")
        layout.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        hand_cursor_on_buttons(buttons)
        ok = dlg.exec_() == QDialog.Accepted
        return edit.text(), ok
