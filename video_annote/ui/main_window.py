# video_annote/ui/main_window.py
"""Main window: data root, sessions, synchronized playback, label capture, review and export."""
from __future__ import annotations

import dataclasses
import math
import os
import time
from datetime import timedelta
from typing import List, Optional, Tuple

from PyQt5.QtCore import QByteArray, Qt, QUrl
from PyQt5.QtGui import QDesktopServices, QFontDatabase, QKeySequence
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QShortcut, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from ..core.colors import color_for_step
from ..core.models import AnnotationRecord, RootConfig, SessionState, SkillStep, encode_camid
from ..core.storage import (
    SessionMovedError, StorageError, backup_file, load_root_config, load_session_state, persist_session_state,
    save_recovery_copy, save_root_config, session_exists, tsv_path,
)
from ..core.timecode import (
    displayed_frame, frame_at_ms, frame_centre_ms, ms_to_precise_str, ms_to_time_str, recompute_from_times,
    seconds_to_frames,
)
from .. import __version__
from ..media.export import export_segments, left_out_outputs
from ..media.ffmpeg import FFmpegError, FFmpegNotFoundError
from ..media.recorded import EnteredTime, local_text, precision_of, start_of
from ..sessions import (
    create_session, frame_mismatch_note, refresh_video_facts, set_recording_start, sync_label_frames,
)
from .common import ElidedLabel, TextFieldsKeepKeys, app_settings, calm_combos, hand_cursor, show_error, split_error
from .dialogs.assessment import ask_confidence_and_notes
from .dialogs.create_session import CreateSessionDialog
from .dialogs.export_dialog import ExportDialog
from .dialogs.import_session import ImportSessionDialog
from .dialogs.session_info import SessionInfoDialog
from .video_grid import VideoGrid
from .widgets.annotations_table import AnnotationsTable
from .widgets.checkable_combo import CheckableComboBox
from .widgets.range_slider import OverlayRange, RangeOverlaySlider
from .widgets.skill_timeline import SkillTimeline
from .widgets.skills_panel import SkillsPanel
from .workers import run_with_progress


def _same_folder(a: Optional[str], b: Optional[str]) -> bool:
    try:
        return bool(a and b) and os.path.samefile(str(a), str(b))
    except OSError:
        return False


def _precise(seconds: Optional[float]) -> str:
    """A stored time (seconds) as shown in the UI: MM:SS.mmm, rounded like label.tsv."""
    return ms_to_precise_str(int(round(float(seconds or 0.0) * 1000.0)))


def _keys(sequence: str) -> str:
    """A shortcut as this platform writes it (⌘O on macOS, Ctrl+O elsewhere)."""
    return QKeySequence(sequence).toString(QKeySequence.NativeText)


APP_TITLE = "Video-Annote"
UNDO_DEPTH = 100
# keys handled by the window (labels and playback); typing into a text field is never affected
SHORTCUTS = [
    ("Space", "Play / pause"),
    ("← / →", "Previous / next frame"),
    ("Shift+← / Shift+→", "Back / forward 1 second"),
    ("Home / End", "First / last frame"),
    ("Click the progress bar", "Go there (not while a label is being recorded)"),
    ("I", "Start recording the selected label (press again to confirm the frame on screen as its start)"),
    ("O", "End the label (press again to confirm the frame on screen as its end)"),
    ("Esc", "Cancel the label being recorded, or a timeline edit"),
    ("Return", "Save a timeline edit"),
    ("Click a timeline block", "Select it and show its first frame (double-click: details, edit, delete)"),
    ("Double-click a table row", "Show its first frame"),
    ("[ / ]", "Show the first / last frame of the selected annotation"),
    ("Shift+I / Shift+O", "Set the selected annotation's start / end to the frame on screen"),
    ("Delete / Backspace", "Delete the selected annotation (asks first)"),
]


class MainWindow(QMainWindow):
    def __init__(self, root_dir: Optional[str] = None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)

        self.root_dir: Optional[str] = None
        self.cfg: Optional[RootConfig] = None
        self._cfg_frozen = False  # config.json could be neither read nor copied: never overwrite it
        self._recovery_dir: Optional[str] = None  # where labels go once the session folder was replaced
        self._close_note = ""
        self.state = SessionState()
        self._skill_selected: Optional[SkillStep] = None
        self._scrubbing = False
        self._scrub_was_playing = False
        self._drag_was_playing = False
        self._timeline_dragging = False
        self._capture_was_playing = False
        self._pending_start_s: Optional[float] = None
        self._warned_mismatch_for: Optional[str] = None
        self._session_notes: List[str] = []
        # label changes that can be undone: (what was done, the annotations before it)
        self._undo: List[Tuple[str, List[AnnotationRecord]]] = []
        self._redo: List[Tuple[str, List[AnnotationRecord]]] = []

        self._build_ui()
        self._build_menus()
        self._install_shortcuts()
        self._restore_layout()
        self._reset_capture()
        self.set_root_dir(root_dir)

    # =====================================================================
    # UI
    # =====================================================================

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(6, 6, 6, 6)
        main.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        main.addLayout(top)
        root_box = QGroupBox("Data Root")
        root_lay = QHBoxLayout(root_box)
        root_lay.setContentsMargins(6, 6, 6, 6)
        self.root_label = ElidedLabel("Not set")
        self.btn_set_root = QPushButton("Select Root…")
        self.btn_set_root.setToolTip("Choose the folder that holds the sessions and the label list (config.json)")
        self.btn_set_root.clicked.connect(self._choose_root_dir)
        root_lay.addWidget(self.root_label, stretch=1)
        root_lay.addWidget(self.btn_set_root)
        top.addWidget(root_box, stretch=2)

        sess_box = QGroupBox("Session")
        sess_lay = QHBoxLayout(sess_box)
        sess_lay.setContentsMargins(6, 6, 6, 6)
        self.btn_create = QPushButton("New Session…")
        self.btn_import = QPushButton("Open Session…")
        self.btn_info = QPushButton("Session Info")
        self.btn_create.setToolTip(f"Import videos into a new session ({_keys('Ctrl+N')})")
        self.btn_import.setToolTip(f"Open a session of the data root ({_keys('Ctrl+O')})")
        self.btn_info.setToolTip(f"Videos, recording times and import notes of this session ({_keys('Ctrl+I')})")
        self.btn_create.clicked.connect(self._create_new_session_dialog)
        self.btn_import.clicked.connect(self._import_existing_session_dialog)
        self.btn_info.clicked.connect(lambda: self._show_session_info())
        self.session_label = ElidedLabel("No session open")
        sess_lay.addWidget(self.btn_create)
        sess_lay.addWidget(self.btn_import)
        sess_lay.addWidget(self.btn_info)
        sess_lay.addSpacing(8)
        sess_lay.addWidget(self.session_label, stretch=1)
        top.addWidget(sess_box, stretch=3)

        # player, timeline and labels above; the annotations table below (both resizable)
        self.vsplit = QSplitter(Qt.Vertical)
        self.vsplit.setChildrenCollapsible(False)
        main.addWidget(self.vsplit, stretch=1)
        split = self.hsplit = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.vsplit.addWidget(split)
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        view_bar = QHBoxLayout()
        view_bar.setSpacing(8)
        self.combo_views = CheckableComboBox()
        self.combo_views.setMinimumWidth(170)
        self.combo_views.checked_ids_changed.connect(self._on_active_views_changed)
        self.combo_views.empty_selection_attempted.connect(
            lambda: self._status("At least one video must stay shown."))
        self.combo_views.setToolTip("Videos shown; labels record the views shown when they are made")
        self.combo_time_source = QComboBox()
        self.combo_audio_source = QComboBox()
        self.combo_time_source.currentIndexChanged.connect(
            lambda _i: self._on_time_source_changed(self.combo_time_source.currentData() or ""))
        self.combo_audio_source.currentIndexChanged.connect(
            lambda _i: self._on_audio_source_changed(self.combo_audio_source.currentData() or ""))
        for combo in (self.combo_time_source, self.combo_audio_source):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(9)  # long recording names are shortened here, full in the list
        calm_combos(self.combo_views, self.combo_time_source, self.combo_audio_source)
        view_bar.addWidget(QLabel("Camera Views:"))
        view_bar.addWidget(self.combo_views)
        view_bar.addSpacing(12)
        view_bar.addWidget(QLabel("Time Source:"))
        view_bar.addWidget(self.combo_time_source)
        view_bar.addSpacing(12)
        view_bar.addWidget(QLabel("Audio Source:"))
        view_bar.addWidget(self.combo_audio_source)
        view_bar.addStretch()
        left_lay.addLayout(view_bar)

        self.video_grid = VideoGrid()
        self.video_grid.position_changed.connect(self._on_position)
        self.video_grid.duration_changed.connect(self._on_duration)
        self.video_grid.playing_changed.connect(lambda _p: self._update_transport_buttons())
        self.video_grid.reached_end.connect(self._update_transport_buttons)
        self.video_grid.ready_changed.connect(self._on_grid_ready)
        left_lay.addWidget(self.video_grid, stretch=1)

        play_bar = QHBoxLayout()
        play_bar.setSpacing(8)
        self.btn_play = QPushButton("Play")
        self.btn_pause = QPushButton("Pause")
        self.btn_restart = QPushButton("Restart")
        self.btn_play.setToolTip("Play (Space)")
        self.btn_pause.setToolTip("Pause (Space)")
        self.btn_restart.setToolTip("Back to the first frame (Home)")
        self.btn_play.clicked.connect(self._play)
        self.btn_pause.clicked.connect(self._pause)
        self.btn_restart.clicked.connect(self._restart)
        digits = QFontDatabase.systemFont(QFontDatabase.FixedFont)  # the readout does not jitter while playing
        self.timeline_label = QLabel("00:00 / 00:00")
        self.timeline_label.setFont(digits)
        self.frame_label = QLabel("")
        self.frame_label.setFont(digits)
        self.frame_label.setStyleSheet("color: #888;")
        for w in (self.btn_play, self.btn_pause, self.btn_restart):
            play_bar.addWidget(w)
        play_bar.addSpacing(12)
        play_bar.addWidget(self.timeline_label)
        play_bar.addSpacing(12)
        play_bar.addWidget(self.frame_label)
        play_bar.addSpacing(12)
        play_bar.addStretch(1)
        self.btn_shortcuts = QToolButton()
        self.btn_shortcuts.setText("Keyboard Shortcuts")
        self.btn_shortcuts.setAutoRaise(True)  # a quiet link-like button
        self.btn_shortcuts.setToolTip("Space play/pause · ←/→ one frame · I start a label · O end it · Esc cancel")
        self.btn_shortcuts.clicked.connect(lambda: self._show_shortcuts())
        play_bar.addWidget(self.btn_shortcuts)
        left_lay.addLayout(play_bar)

        self.slider = RangeOverlaySlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.seek_requested.connect(self._on_slider_clicked)
        left_lay.addWidget(self.slider)

        timeline_box = QGroupBox("Timeline")
        tl_lay = QVBoxLayout(timeline_box)
        tl_lay.setContentsMargins(6, 6, 6, 6)
        self.skill_timeline = SkillTimeline()
        self.skill_timeline.request_edit.connect(self._on_timeline_request_edit)
        self.skill_timeline.request_delete.connect(self._delete_record)
        self.skill_timeline.edit_preview.connect(self._on_timeline_edit_preview)
        self.skill_timeline.edit_committed.connect(self._on_timeline_edit_committed)
        self.skill_timeline.edit_canceled.connect(self._on_timeline_edit_canceled)
        self.skill_timeline.drag_started.connect(self._on_timeline_drag_started)
        self.skill_timeline.drag_finished.connect(self._on_timeline_drag_finished)
        self.skill_timeline.record_clicked.connect(self._on_timeline_record_clicked)
        tl_lay.addWidget(self.skill_timeline)
        left_lay.addWidget(timeline_box)

        step_bar = QHBoxLayout()
        step_bar.setSpacing(8)
        self.btn_start_step = QPushButton("Start")
        self.btn_confirm_start = QPushButton("Confirm Start")
        self.btn_end_step = QPushButton("End")
        self.btn_finish_step = QPushButton("Confirm End")
        self.btn_cancel_step = QPushButton("Cancel Label")
        self.btn_start_step.setToolTip("Start recording the label selected on the right (I)")
        self.btn_confirm_start.setToolTip("The frame on screen is the label's first frame (I)")
        self.btn_end_step.setToolTip("Stop here to pick the label's last frame (O)")
        self.btn_finish_step.setToolTip("The frame on screen is the label's last frame: save the label (O)")
        self.btn_cancel_step.setToolTip("Discard the label being recorded (Esc)")
        self.btn_start_step.clicked.connect(self._start_step)
        self.btn_confirm_start.clicked.connect(self._confirm_start)
        self.btn_end_step.clicked.connect(self._end_step)
        self.btn_finish_step.clicked.connect(self._finish_step)
        self.btn_cancel_step.clicked.connect(self._cancel_step)
        for w in (self.btn_start_step, self.btn_confirm_start, self.btn_end_step, self.btn_finish_step,
                  self.btn_cancel_step):
            step_bar.addWidget(w)
        step_bar.addStretch()
        left_lay.addLayout(step_bar)
        self.current_step_label = QLabel("Current step: None")
        self.current_step_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        left_lay.addWidget(self.current_step_label)

        self.skills_panel = SkillsPanel()
        self.skills_panel.skill_selected.connect(self._on_skill_selected)
        self.skills_panel.skills_changed.connect(self._on_skills_changed)
        split.addWidget(left)
        split.addWidget(self.skills_panel)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)

        bottom = QWidget()
        bottom_lay = QVBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        self.table = AnnotationsTable()
        self.table.edit_requested.connect(self._apply_record_edit)
        self.table.delete_requested.connect(self._delete_record)
        self.table.record_selected.connect(self.skill_timeline.set_selected)
        self.table.record_activated.connect(self._show_record)
        self.table.set_edge_requested.connect(self._set_edge_here)
        self.table.go_to_requested.connect(lambda rec, edge: (self.table.select_record(rec), self._go_to_edge(edge)))
        bottom_lay.addWidget(self.table, stretch=1)
        actions = QHBoxLayout()
        self.btn_delete_record = QPushButton("Delete Annotation")
        self.btn_delete_record.setToolTip("Delete the annotation selected in the table (Delete)")
        self.btn_delete_record.clicked.connect(self.table.request_delete_selected)
        self.btn_export = QPushButton("Export Clips…")
        self.btn_export.setToolTip(f"Write the labeled clips (MP4/WAV) and a manifest ({_keys('Ctrl+E')})")
        self.btn_export.clicked.connect(self._export_clips)
        self.btn_finish_session = QPushButton("Finish Session")
        self.btn_finish_session.setToolTip("Save and close this session")
        self.btn_finish_session.clicked.connect(self._finish_session)
        actions.addWidget(self.btn_delete_record)
        actions.addStretch()
        actions.addWidget(self.btn_export)
        actions.addWidget(self.btn_finish_session)
        bottom_lay.addLayout(actions)
        self.vsplit.addWidget(bottom)
        self.vsplit.setStretchFactor(0, 3)
        self.vsplit.setStretchFactor(1, 1)

        hand_cursor(self.btn_set_root, self.btn_create, self.btn_import, self.btn_info, self.btn_play, self.btn_pause,
                    self.btn_restart, self.btn_start_step, self.btn_confirm_start, self.btn_end_step,
                    self.btn_finish_step, self.btn_cancel_step, self.btn_delete_record, self.btn_export, self.btn_finish_session,
                    self.combo_views, self.combo_time_source, self.combo_audio_source)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.act_new = file_menu.addAction("New Session…", self._create_new_session_dialog, QKeySequence.New)
        self.act_open = file_menu.addAction("Open Session…", self._import_existing_session_dialog, QKeySequence.Open)
        file_menu.addAction("Select Data Root…", self._choose_root_dir)
        file_menu.addSeparator()
        self.act_info = file_menu.addAction("Session Info", lambda: self._show_session_info(), QKeySequence("Ctrl+I"))
        self.act_export = file_menu.addAction("Export Clips…", self._export_clips, QKeySequence("Ctrl+E"))
        self.act_finish = file_menu.addAction("Finish Session", self._finish_session)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.close, QKeySequence.Quit)
        edit_menu = self.menuBar().addMenu("&Edit")
        self.act_undo = edit_menu.addAction("Undo", self._undo_change, QKeySequence.Undo)
        self.act_redo = edit_menu.addAction("Redo", self._redo_change, QKeySequence.Redo)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Keyboard Shortcuts", self._show_shortcuts, QKeySequence.HelpContents)
        help_menu.addAction("About Video-Annote", self._show_about)

    def _install_shortcuts(self) -> None:
        """Single keys are window shortcuts, never menu shortcuts: text fields that have the focus keep
        their keys (typing a label name), and a native menu bar never takes them."""
        def add(key: str, fn) -> QShortcut:
            return QShortcut(QKeySequence(key), self, activated=fn)

        add("Space", self._toggle_play)
        add("Left", lambda: self._step(-1))
        add("Right", lambda: self._step(1))
        add("Shift+Left", lambda: self._jump_ms(-1000))
        add("Shift+Right", lambda: self._jump_ms(1000))
        add("Home", lambda: self._jump_to(0))
        add("End", lambda: self._jump_to(self.video_grid.duration()))
        add("I", self._key_start)
        add("O", self._key_end)
        add("Delete", self._delete_selected)
        add("Backspace", self._delete_selected)
        add("[", lambda: self._go_to_edge("start"))
        add("]", lambda: self._go_to_edge("end"))
        add("Shift+I", lambda: self._set_edge_here(self.table.selected_record(), "start"))
        add("Shift+O", lambda: self._set_edge_here(self.table.selected_record(), "end"))
        # only while there is something to cancel or save: otherwise these keys stay with the focused widget
        self.escape = add("Escape", self._key_escape)
        self.commit_edit = add("Return", lambda: self.skill_timeline.exit_editing(commit=True))
        # ... and a text field that has the focus keeps them (Return adds a label, Esc ends typing)
        QApplication.instance().installEventFilter(TextFieldsKeepKeys(self))

    def _restore_layout(self) -> None:
        settings = app_settings()
        geometry = settings.value("window/geometry")
        if not isinstance(geometry, QByteArray) or not self.restoreGeometry(geometry):
            screen = QApplication.primaryScreen()
            area = screen.availableGeometry() if screen is not None else None
            if area is not None:  # fit the screen (a laptop's is often 1440x900 or smaller)
                self.resize(min(1800, int(area.width() * 0.92)), min(1050, int(area.height() * 0.9)))
                self.move(area.center() - self.rect().center())
            else:
                self.resize(1500, 900)
        restored = {}
        for name, splitter in (("window/main_split", self.vsplit), ("window/side_split", self.hsplit)):
            state = settings.value(name)
            restored[name] = isinstance(state, QByteArray) and splitter.restoreState(state)
        if not restored["window/main_split"]:
            self.vsplit.setSizes([700, 230])
        if not restored["window/side_split"]:  # room for the label names
            self.hsplit.setSizes([int(self.width() * 0.77), int(self.width() * 0.23)])

    def _save_layout(self) -> None:
        settings = app_settings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/main_split", self.vsplit.saveState())
        settings.setValue("window/side_split", self.hsplit.saveState())

    def _update_title(self) -> None:
        self.setWindowTitle(f"{self.state.label} — {APP_TITLE}" if self.state.is_loaded() else APP_TITLE)
        if not self.root_dir:
            hint = "Select a data root (the folder that holds the sessions) to start: Select Root…"
        else:
            hint = (f"Open a session ({_keys('Ctrl+O')}) or create a new one from recordings "
                    f"({_keys('Ctrl+N')}).")
        self.video_grid.set_placeholder(hint)

    def _show_shortcuts(self) -> None:
        rows = "".join(f"<tr><td style='padding-right:16px'><b>{k}</b></td><td>{v}</td></tr>" for k, v in SHORTCUTS)
        commands = [("New / Open Session", f"{_keys('Ctrl+N')} / {_keys('Ctrl+O')}"),
                    ("Session Info / Export Clips", f"{_keys('Ctrl+I')} / {_keys('Ctrl+E')}"),
                    ("Undo / Redo a label change", f"{QKeySequence(QKeySequence.Undo).toString(QKeySequence.NativeText)}"
                                                   f" / {QKeySequence(QKeySequence.Redo).toString(QKeySequence.NativeText)}")]
        rows += "".join(f"<tr><td style='padding-right:16px'><b>{v}</b></td><td>{k}</td></tr>" for k, v in commands)
        box = QMessageBox(QMessageBox.NoIcon, "Keyboard Shortcuts", f"<table>{rows}</table>", QMessageBox.Close, self)
        box.exec_()

    def _show_about(self) -> None:
        try:
            from ..media.ffmpeg import ffmpeg_version
            ffmpeg = ffmpeg_version()
        except Exception:  # (no ffmpeg found: said at start-up)
            ffmpeg = "ffmpeg not found"
        QMessageBox.about(self, "About Video-Annote",
                          f"Video-Annote {__version__}\nMulti-video annotation tool.\n\n{ffmpeg}")

    def _status(self, text: str, ms: int = 4000) -> None:
        self.statusBar().showMessage(text, ms)

    # =====================================================================
    # Root config and labels
    # =====================================================================

    def _choose_root_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Data Root", self.root_dir or "")
        if not d:
            return
        if d == self.root_dir:  # reload the label list (e.g. after fixing config.json); the session stays
            self.set_root_dir(d)
            return
        if self.state.is_loaded() and not self._close_session(ask=True):
            return
        self.set_root_dir(d)

    def set_root_dir(self, root_dir: Optional[str]) -> None:
        self.root_dir = os.path.abspath(root_dir) if root_dir else None
        self._warned_mismatch_for = None
        self.cfg = None
        self._cfg_frozen = False
        if self.root_dir:
            try:
                self.cfg = load_root_config(self.root_dir)
            except StorageError as e:
                self._cfg_frozen = e.backup is None
                self.cfg = RootConfig(root_dir=self.root_dir)
                self.cfg.disk_hash = e.raw_hash  # already copied aside: not "changed elsewhere" later
                self.cfg.saved_state = self.cfg.to_dict()  # an untouched empty list is never written
                kept = (f"A copy was saved to:\n{e.backup}\n\nStarting with an empty labels list; the copy "
                        "is kept so nothing is lost." if e.backup else
                        "No copy could be made, so the file is left untouched: label changes are not saved "
                        "until it can be read (check its permissions and encoding) and the root is selected "
                        "again.")
                QMessageBox.warning(self, "Config could not be read", f"{e}\n\n{kept}")
            if self.cfg is not None and self.cfg.problems:
                QMessageBox.warning(self, "Some labels could not be read",
                                    "These entries of config.json could not be read and are not shown:\n\n"
                                    + "\n".join(f"• {p}" for p in self.cfg.problems[:12]))
            if self.cfg is None:  # no config.json yet: written once labels or colors are added
                self.cfg = RootConfig(root_dir=self.root_dir)
                self.cfg.saved_state = self.cfg.to_dict()
            self.root_label.setText(self.root_dir)
            app_settings().setValue("last_root", self.root_dir)  # opened again at the next start
        else:
            self.root_label.setText("Not set")
        self.skills_panel.set_color_map(self.cfg.skill_color_map if self.cfg else None)
        self.skills_panel.set_skills(self.cfg.skills if self.cfg else [])
        self.table.set_skills(self.cfg.skills if self.cfg else [])
        self.skill_timeline.set_color_resolver(self._color)
        self._update_title()
        self._update_enabled_state()

    def _color(self, step_no: int) -> str:
        return color_for_step(step_no, self.cfg.skill_color_map if self.cfg else None)

    def _on_skill_selected(self, step: Optional[SkillStep]) -> None:
        self._skill_selected = step
        if not self.state.is_capturing:
            self._show_idle_hint()
            self._update_enabled_state()

    def _on_skills_changed(self) -> None:
        if not self.cfg:
            return
        self.cfg.skills = self.skills_panel.skills()
        self.table.set_skills(self.cfg.skills)
        self._skill_selected = self.skills_panel.selected_skill()  # never keep a replaced definition
        self._save_config()
        self._warned_mismatch_for = None

    def _save_config(self) -> bool:
        if not self.cfg:
            return True
        if self._cfg_frozen:
            self._status("Label changes are NOT saved: config.json could not be read (fix it, then select the "
                         "data root again).", 0)
            return True
        skills, colors = list(self.cfg.skills), dict(self.cfg.skill_color_map)
        try:
            kept = save_root_config(self.cfg)
        except (OSError, StorageError) as e:
            QMessageBox.warning(self, "Config save failed", str(e))
            return False
        if self.cfg.skills != skills or self.cfg.skill_color_map != colors:
            # labels or colors added in another window were kept: show them everywhere
            self.skills_panel.set_skills(self.cfg.skills)
            self.table.set_skills(self.cfg.skills)
            self.skill_timeline.set_color_resolver(self._color)  # repaints with the current colors
        if kept:
            QMessageBox.warning(self, "Labels changed elsewhere",
                                "config.json on disk was not the version this window last read or wrote (another "
                                "window or program changed it, or some of its entries could not be read when it was "
                                "loaded). This window's label list was saved; the version that was on disk was kept "
                                "as:\n" + kept)
        return True

    # =====================================================================
    # Sessions
    # =====================================================================

    def _create_new_session_dialog(self) -> None:
        if not self.root_dir:
            QMessageBox.warning(self, "No Root", "Please select a Data Root first.")
            return
        dlg = CreateSessionDialog(self)
        if dlg.exec_() != dlg.Accepted:
            return
        label, sources = dlg.session_label(), dlg.sources()
        exists, path = session_exists(self.root_dir, label)
        replace = False
        if exists:
            resp = QMessageBox.question(
                self, "Session exists",
                f"Session '{label}' already exists:\n{path}\n\nReplace it? The new videos are imported first; "
                "only if that succeeds is the old folder moved (not deleted) to the hidden "
                "'.video-annote-trash' folder in the data root.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return
            replace = True
        # the open session stays open during the import (and if it fails), unless it is the one being
        # replaced: its folder will move, so it is closed first and reopened if nothing changed
        reopen = None
        if self.state.is_loaded():
            if replace and _same_folder(path, self.state.session_dir):
                reopen = self.state.label
                if not self._close_session(ask=True):
                    return
            else:
                if self.state.is_capturing and QMessageBox.question(
                        self, "Unfinished label", "A label is being recorded. Discard it and create the new "
                        "session?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                    return
                self.video_grid.pause()

        root = self.root_dir
        outcome = run_with_progress(
            self, "Importing videos", "Preparing…",
            lambda report, cancelled: create_session(root, label, sources, replace=replace,
                                                     on_progress=report, should_cancel=cancelled))
        if not outcome.ok:
            if reopen:
                self._open_session(reopen)
            if outcome.status == "cancelled":
                self._status("Import cancelled; nothing was changed.")
            else:
                head, details = split_error(outcome.error)
                show_error(self, "Import failed", f"The session was not created.\n\n{head}", details)
            return
        if self.state.is_loaded() and not self._close_session(ask=False):
            self._status(f"Session '{label}' was created; open it with Open Session… ({_keys('Ctrl+O')}).", 0)
            return
        result = outcome.value
        app_settings().setValue("last_session", label)
        self.state = result.state
        # per-video import warnings are stored with each video and listed by Session Info
        self._session_notes = [f"previous session moved to {result.replaced_to}"] if result.replaced_to else []
        self._load_session_into_ui()
        if result.notes or self._session_notes:  # only when there is something worth reading
            self._show_session_info(title="Import finished")
        else:
            self._status(f"Session '{label}' created.")

    def _import_existing_session_dialog(self) -> None:
        if not self.root_dir:
            QMessageBox.warning(self, "No Root", "Please select a Data Root first.")
            return
        dlg = ImportSessionDialog(self.root_dir, self, current=app_settings().value("last_session", "", str))
        if dlg.exec_() != dlg.Accepted or not dlg.selected_label():
            return
        if self.state.is_loaded() and not self._close_session(ask=True):
            return
        self._open_session(dlg.selected_label())

    def _open_session(self, label: str) -> bool:
        self._warned_mismatch_for = None  # warn about label-list mismatches on every open, as 0.1.x did
        try:
            state, report = load_session_state(self.root_dir or "", label)
        except (OSError, UnicodeDecodeError, StorageError) as e:
            show_error(self, "Cannot open session", f"The session could not be read:\n{e}")
            return False
        notes = list(report.warnings)
        if report.skipped_rows:
            kept = [b for b in report.backups if os.path.basename(b).startswith("label.")]
            notes.append(
                f"{len(report.skipped_rows)} row(s) of label.tsv could not be read (lines "
                f"{', '.join(str(n) for n, _ in report.skipped_rows)}) and are not shown. "
                + (f"A copy of the original file was saved to {kept[0]}." if kept else
                   "No copy of the file could be made, so the session cannot be saved until one can be (check "
                   "the hidden .video-annote-backups folder next to it)."))
        no_ffprobe = False
        try:
            notes += refresh_video_facts(state)
        except (FFmpegNotFoundError, FFmpegError) as e:
            no_ffprobe = isinstance(e, FFmpegNotFoundError)
            notes.append(f"videos could not be measured ({str(e).splitlines()[0]}); frame rates come from "
                         "session.json where it states them")
        unknown = [v.video_id for v in state.videos if not v.rate_known]
        if unknown:
            notes.append(f"the frame rate of {', '.join(unknown)} is unknown"
                         + (" (ffmpeg/ffprobe is needed to measure it)" if no_ffprobe else "")
                         + ": labels cannot be recorded or retimed with it as time source"
                         + (" until ffmpeg is installed and the session is opened again" if no_ffprobe else ""))
        rows = sync_label_frames(state)
        if rows:  # the times are kept; the old frame columns are kept in a copy
            try:
                kept = backup_file(tsv_path(state.session_dir or ""))
            except OSError:
                kept = None
            notes.append(frame_mismatch_note(
                rows, "they were recomputed from the times, which the timeline shows and clips are cut from, and are "
                      "saved with the next change or when the session is finished"
                      + (f" (a copy of the original file was saved to {kept})" if kept else "")))
        self.state = state
        self._session_notes = notes
        self._load_session_into_ui()
        app_settings().setValue("last_session", label)
        if report.skipped_rows or report.backups or notes:
            self._show_session_info(title="Session opened with warnings")
        return True

    def _load_session_into_ui(self) -> None:
        self._recovery_dir = None
        self._undo.clear()
        self._redo.clear()
        self.session_label.setText(self.state.label or "")
        self.session_label.setToolTip(self.state.session_dir or "")
        self.video_grid.load(self.state.session_dir or "", self.state.videos)
        self._populate_video_selectors()
        self.video_grid.set_time_source(self.state.time_source_id or "")
        self.video_grid.set_audio_source(self.state.audio_source_id or "")
        self._on_duration(self.video_grid.duration())
        self.video_grid.seek(0)
        self.table.set_records(self.state.annotations)
        self._refresh_annotations_view()
        self._warn_if_session_skills_mismatch()
        self._reset_capture()
        self._update_title()
        self._update_enabled_state()
        self.skills_panel.list.setFocus()

    def _populate_video_selectors(self) -> None:
        ids = self.state.video_ids()
        labels = [v.display_name for v in self.state.videos]
        for combo, current in ((self.combo_time_source, self.state.time_source_id),
                               (self.combo_audio_source, self.state.audio_source_id)):
            combo.blockSignals(True)
            combo.clear()
            for vid, label in zip(ids, labels):
                combo.addItem(label, vid)
            if current in ids:
                combo.setCurrentIndex(combo.findData(current))
            combo.view().setMinimumWidth(combo.view().sizeHintForColumn(0) + 24)
            combo.blockSignals(False)
        self.combo_views.set_items(ids, labels)
        checked = [v for v in self.state.active_view_ids if v in ids] or ids[:1]
        self.combo_views.set_checked_ids(checked, emit_signal=False)
        self.video_grid.set_active_views(checked)
        self.state.active_view_ids = self.video_grid.active_views()

    def _show_session_info(self, title: str = "Session Info") -> None:
        if not self.state.is_loaded():
            return
        SessionInfoDialog(self.state, self._session_notes, title, self, set_start=self._set_recording_start).exec_()

    def _set_recording_start(self, video_id: str, entered: EnteredTime) -> None:
        """Set (or, with an empty one, forget) a video's recording start typed by hand, and save."""
        try:
            set_recording_start(self.state, video_id, entered)
        except (FFmpegNotFoundError, FFmpegError) as e:  # (going back to the file's time reads the file)
            raise ValueError(f"The file's own time could not be read: {str(e).splitlines()[0]}") from e
        self._save(quiet=True)
        self._update_time_label(self.video_grid.position())

    def _close_session(self, ask: bool) -> bool:
        """Save and unload the current session. Returns False if the user chose to keep it open."""
        if not self.state.is_loaded():
            return True
        if self.state.is_capturing and ask:
            resp = QMessageBox.question(
                self, "Unfinished label",
                "A label is being recorded (Start confirmed, End not saved). Discard it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return False
        if self._save(quiet=True):
            self._close_note = ("Session saved and closed." if self._recovery_dir is None else
                                f"Session closed. Its labels were saved to {self._recovery_dir}, not into the session "
                                "folder (see the earlier message).")
        else:
            resp = QMessageBox.question(
                self, "Save failed",
                "The session could not be saved (see the previous message). Close it anyway and lose "
                "unsaved changes?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return False
            self._close_note = "Session closed without saving its latest changes."
        self._recovery_dir = None
        self.skill_timeline.exit_editing(commit=False)
        self.video_grid.clear()
        self.statusBar().clearMessage()  # clear() reported "Preparing videos…" while still loaded
        self.slider.setRange(0, 0)
        self.slider.clear_overlays()
        self.state = SessionState(root_dir=self.root_dir)
        self._session_notes = []
        self._undo.clear()
        self._redo.clear()
        self.session_label.setText("No session open")
        self.table.set_records([])
        self.skill_timeline.set_annotations([])
        self.skill_timeline.set_duration_ms(0)
        self.skill_timeline.set_playhead_ms(0)
        self._reset_capture()
        self._update_title()
        self._update_enabled_state()
        return True

    def _finish_session(self) -> None:
        if self._close_session(ask=True):
            if self._close_note == "Session saved and closed.":
                self._status(f"Session saved and closed. Open another one ({_keys('Ctrl+O')}) or create one "
                             f"({_keys('Ctrl+N')}).", 8000)
            else:  # saved elsewhere, or not saved: worth a message the user has to see
                QMessageBox.information(self, "Session finished",
                                        f"{self._close_note} You can open or create another session.")

    def _warn_if_session_skills_mismatch(self) -> None:
        if not self.state.is_loaded() or not self.cfg or self._warned_mismatch_for == self.state.label:
            return
        self._warned_mismatch_for = self.state.label
        cfg_map = {s.number: s.name for s in self.cfg.skills}
        missing, renamed, seen = [], [], set()
        for rec in self.state.annotations:
            if rec.step_no in seen:
                continue
            seen.add(rec.step_no)
            if rec.step_no not in cfg_map:
                missing.append(f"  • {rec.step_no}: {rec.step_name}")
            elif rec.step_name and rec.step_name != cfg_map[rec.step_no]:
                renamed.append(f"  • {rec.step_no}: session '{rec.step_name}' / config '{cfg_map[rec.step_no]}'")
        if not missing and not renamed:
            return
        parts = ["Some labels in this session do not match the current Labels list (config.json). "
                 "The annotations still show on the timeline; add or rename labels on the right to keep "
                 "them consistent."]
        if missing:
            parts += ["", "Missing from the Labels list:"] + missing
        if renamed:
            parts += ["", "Same number, different name:"] + renamed
        QMessageBox.warning(self, "Labels mismatch", "\n".join(parts))

    # =====================================================================
    # Saving
    # =====================================================================

    def _save(self, quiet: bool = False) -> bool:
        if not self.state.is_loaded():
            return True
        try:
            if self._recovery_dir is not None:  # the session folder is gone: keep the recovery copy current
                folder = save_recovery_copy(self.state, self._recovery_dir)
                if folder != self._recovery_dir:  # e.g. the data root was renamed or moved too
                    QMessageBox.warning(self, "Recovery folder moved",
                                        f"The folder this window's labels were being saved to no longer exists (was the "
                                        f"data root renamed or moved?), so they are now saved to:\n{folder}\n\nThe "
                                        f"earlier copy was in:\n{self._recovery_dir}")
                    self._recovery_dir = folder
                kept = []
            else:
                kept = persist_session_state(self.state)
        except SessionMovedError as e:
            try:
                self._recovery_dir = save_recovery_copy(self.state)
            except OSError as e2:
                self._status(f"SAVE FAILED: {e2}", 0)
                QMessageBox.warning(self, "Autosave failed", f"{e}\n\nThe labels could not be saved elsewhere "
                                    f"either:\n{e2}\n\nThey are kept in memory.")
                return False
            # how to bring the labels back into a session: open it, finish it once in this version (so it
            # uses the current format), then copy label.tsv from the recovery folder into it
            QMessageBox.warning(
                self, "Session folder replaced or moved",
                f"{e}\n\nTo avoid writing into the wrong place, this window's labels and settings were saved to:\n"
                f"{self._recovery_dir}\n\nFinish this session. If its folder was renamed or moved, open it from its "
                "new location, finish it once, then copy label.tsv from the folder above into it. If the session "
                "was replaced (for example by an import that replaced it), the original was moved to the hidden "
                "'.video-annote-trash' folder of the data root: these labels belong to it, not to the new session "
                "(unless that was imported from the same recordings in the same order); move the original back "
                "into the data root under a new name, open it, finish it once, then copy label.tsv into it. If the "
                "folder was deleted, import the same recordings again, in the same order, into a new session, "
                "finish it once, then copy label.tsv into it.")
            self._status(f"Labels are saved to {self._recovery_dir}", 0)
            return True
        except OSError as e:
            self._status(f"SAVE FAILED: {e}", 0)
            QMessageBox.warning(self, "Autosave failed",
                                f"Changes could not be written to {self.state.session_dir}:\n{e}\n\n"
                                "They are kept in memory; fix the problem and make any change to retry.")
            return False
        if kept:
            QMessageBox.warning(
                self, "Session changed elsewhere",
                "This session's files on disk were not the version this window last read or wrote (the same "
                "session open in another window, another program, or a file that could not be read when it "
                "was opened). This window's version was saved; the version that was on disk was kept as:\n"
                + "\n".join(kept) + "\n\nAvoid working on the same session in two places at once.")
        if not quiet:
            where = f" to {self._recovery_dir} (not into the session folder)" if self._recovery_dir else ""
            self._status(f"Saved {time.strftime('%H:%M:%S')}{where}", 0 if where else 4000)
        return True

    # =====================================================================
    # Playback
    # =====================================================================

    def _play(self) -> None:
        if self.state.is_loaded():
            self.video_grid.play()
            self._update_transport_buttons()

    def _pause(self) -> None:
        self.video_grid.pause()
        self._update_transport_buttons()

    def _toggle_play(self) -> None:
        if self.video_grid.is_playing():
            self._pause()
        else:
            self._play()

    def _restart(self) -> None:
        if not self.state.is_loaded():
            return
        was_playing = self.video_grid.is_playing()
        self.video_grid.pause()
        self.video_grid.seek(0)
        if was_playing:
            self.video_grid.play()
        self._update_transport_buttons()

    def _step(self, frames: int) -> None:
        if not self.state.is_loaded() or self._scrubbing or not self.video_grid.is_ready():
            return
        if self.video_grid.is_playing():
            self.video_grid.pause()
        self.video_grid.step_frames(frames)
        self._update_transport_buttons()

    def _jump_ms(self, delta: int) -> None:
        if not self.state.is_loaded() or self._scrubbing or not self.video_grid.is_ready():
            return
        self._jump_to(self.video_grid.position() + delta)

    def _jump_to(self, position_ms: int) -> None:
        """Pause and show the frame at ``position_ms`` (clamped to the time source's frames)."""
        if not self.state.is_loaded() or self._scrubbing or not self.video_grid.is_ready():
            return
        if self.video_grid.is_playing():
            self.video_grid.pause()
        self.video_grid.seek(max(0, int(position_ms)))
        self._update_transport_buttons()

    def _on_duration(self, dur_ms: int) -> None:
        self.slider.setRange(0, max(0, int(dur_ms)))
        self.skill_timeline.set_duration_ms(int(dur_ms))
        self._update_time_label(self.video_grid.position())

    def _on_position(self, pos_ms: int) -> None:
        if self._scrubbing:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(int(pos_ms))
        self.slider.blockSignals(False)
        self._update_time_label(pos_ms)
        self.skill_timeline.set_playhead_ms(pos_ms)
        self._refresh_pending_overlay(pos_ms)
        self._update_transport_buttons()

    def _update_time_label(self, pos_ms: int) -> None:
        dur = self.video_grid.duration() if self.state.is_loaded() else 0
        self.timeline_label.setText(f"{ms_to_time_str(pos_ms)} / {ms_to_time_str(dur)}")
        ts = self.video_grid.time_source() if self.state.is_loaded() else None
        item = self.state.get_video(ts) if ts else None
        if item is not None and not item.rate_known:  # frame numbers would be guesses
            self.frame_label.setText(f"{ms_to_precise_str(pos_ms)}  ·  frame rate unknown")
        elif ts:
            frame, start_s, _ = self._snap_ms(pos_ms)
            text = f"{_precise(start_s)}  ·  frame {frame}"
            start = start_of(item.recorded) if item is not None else None
            tip = ""
            if start is not None:  # the real-world time of this frame, from the recording's own metadata
                precision = precision_of(item.recorded)
                text += f"  ·  {local_text(start + timedelta(seconds=start_s), precision, date=False)}"
                tip = f"Real-world time of this frame (recording time {item.recorded.get('from')})"
            if self.frame_label.toolTip() != tip:
                self.frame_label.setToolTip(tip)
            self.frame_label.setText(text)
        else:
            self.frame_label.setText("")

    def _on_slider_pressed(self) -> None:
        if not self.state.is_loaded():
            return
        self._scrubbing = True
        self._scrub_was_playing = self.video_grid.is_playing()
        if self._scrub_was_playing:
            self.video_grid.pause()

    def _on_slider_moved(self, value: int) -> None:
        if not self.state.is_loaded():
            return
        self.video_grid.seek(int(value))
        self._update_time_label(int(value))
        self.skill_timeline.set_playhead_ms(int(value))
        self._refresh_pending_overlay(int(value))

    def _on_slider_released(self) -> None:
        if not self.state.is_loaded():
            self._scrubbing = False
            return
        self._scrubbing = False
        self.video_grid.seek(self.slider.value())
        if self._scrub_was_playing:
            self.video_grid.play()
        self._scrub_was_playing = False
        self._update_transport_buttons()

    def _on_slider_clicked(self, value: int) -> None:
        """A click on the bar goes there (playing on if playing); not while a label is being recorded."""
        if self.state.is_loaded() and self.video_grid.is_ready() and not self.state.is_capturing:
            self.video_grid.seek(int(value))
            self._update_transport_buttons()

    def _on_grid_ready(self, ready: bool) -> None:
        failed = self.video_grid.failed_videos() if ready else []
        if failed:
            self._status(f"Could not open {', '.join(failed)}.", 0)
            QMessageBox.warning(self, "Videos could not be opened",
                                f"{', '.join(failed)} could not be opened by the player (missing or unreadable "
                                "working copy). Check Session Info and the session folder.")
        elif ready:
            self._status("Videos ready.", 2000)
        elif self.state.is_loaded():
            self._status("Preparing videos…", 0)
        self._update_enabled_state()

    def _update_transport_buttons(self) -> None:
        loaded = self.state.is_loaded() and self.video_grid.is_ready()
        playing = loaded and self.video_grid.is_playing()
        self.btn_play.setEnabled(loaded and not playing and not self.video_grid.at_end())
        self.btn_pause.setEnabled(playing)
        self.btn_restart.setEnabled(loaded)

    # =====================================================================
    # Views / time source / audio source
    # =====================================================================

    def _on_active_views_changed(self, views: List[str]) -> None:
        if not self.state.is_loaded():
            return
        views = [v for v in views if v in self.state.video_ids()]
        if not views:
            return
        self.video_grid.set_active_views(views)
        self.state.active_view_ids = self.video_grid.active_views()
        self._save(quiet=True)

    def _revert_combo(self, combo: QComboBox, value: Optional[str]) -> None:
        combo.blockSignals(True)
        combo.setCurrentIndex(combo.findData(value or ""))
        combo.blockSignals(False)

    def _on_time_source_changed(self, vid: str) -> None:
        if not self.state.is_loaded() or not vid:
            return
        if self.state.pending_start_ms is not None:  # recorded at Confirm Start
            QMessageBox.information(self, "Time source locked",
                                    "Finish or cancel the current label before changing the time source.")
            self._revert_combo(self.combo_time_source, self.video_grid.time_source())
            return
        was_playing = self.video_grid.is_playing()
        self.video_grid.pause()
        self.video_grid.set_time_source(vid)
        self.state.time_source_id = vid
        self._on_duration(self.video_grid.duration())
        self._on_position(self.video_grid.position())
        if was_playing:
            self.video_grid.play()
        self._update_transport_buttons()
        self._save(quiet=True)

    def _on_audio_source_changed(self, vid: str) -> None:
        if not self.state.is_loaded() or not vid:
            return
        if self.state.pending_start_ms is not None:  # recorded at Confirm Start
            QMessageBox.information(self, "Audio source locked",
                                    "Finish or cancel the current label before changing the audio source.")
            self._revert_combo(self.combo_audio_source, self.video_grid.audio_source())
            return
        self.video_grid.set_audio_source(vid)
        self.state.audio_source_id = vid
        self._save(quiet=True)

    # =====================================================================
    # Label capture: Start -> Confirm Start -> End -> Confirm End
    # =====================================================================

    def _reset_capture(self) -> None:
        self.state.clear_pending()
        self._pending_start_s = None
        self._capture_was_playing = False
        self._show_idle_hint()
        self.slider.set_overlays([])
        self.slider.clear_edit_overlay()
        self._set_capture_buttons(start=True)
        self.skill_timeline.set_allow_edit(True)

    def _show_idle_hint(self) -> None:
        step = self._skill_selected
        if not self.state.is_loaded():
            text = "Current step: None"
        elif step is None:
            text = "Current step: None. Select a label in the Labels list on the right to record it."
        else:
            text = f"Current step: None. Next: {step.number} - {step.name} (Start, or press I)"
        self.current_step_label.setText(text)

    def _set_capture_buttons(self, start=False, confirm_start=False, end=False, finish=False) -> None:
        loaded = self.state.is_loaded()
        self.btn_start_step.setEnabled(loaded and start and self._skill_selected is not None)
        self.btn_confirm_start.setEnabled(loaded and confirm_start)
        self.btn_end_step.setEnabled(loaded and end)
        self.btn_finish_step.setEnabled(loaded and finish)
        self.btn_cancel_step.setEnabled(loaded and self.state.is_capturing)
        self.slider.click_to_seek = loaded and not self.state.is_capturing
        self._update_source_combos()
        self._update_escape()

    def _update_source_combos(self) -> None:
        """The time and audio sources are locked from Confirm Start until the label is saved or cancelled
        (the label records them at Confirm Start)."""
        locked = self.state.pending_start_ms is not None
        for combo, tip in ((self.combo_time_source, "The video whose clock and frame numbers labels are recorded with"),
                           (self.combo_audio_source, "The video you hear (labels record it as their audio source)")):
            combo.setEnabled(self.state.is_loaded() and not locked)
            combo.setToolTip("Locked while a label is being recorded" if locked else tip)

    def _update_escape(self) -> None:
        self.escape.setEnabled(self.state.is_capturing or self.skill_timeline.is_editing())
        self.commit_edit.setEnabled(self.skill_timeline.is_editing())

    def _key_start(self) -> None:
        """I: Start, or Confirm Start once started."""
        if self.btn_confirm_start.isEnabled():
            self._confirm_start()
        elif self.btn_start_step.isEnabled():
            self._start_step()
        elif self.state.is_loaded() and not self.state.is_capturing and self._skill_selected is None:
            self._status("Select a label in the Labels list first.")

    def _key_end(self) -> None:
        """O: End, or Confirm End once ended."""
        if self.btn_finish_step.isEnabled():
            self._finish_step()
        elif self.btn_end_step.isEnabled():
            self._end_step()

    def _key_escape(self) -> None:
        if self.state.is_capturing:
            self._cancel_step()
        elif self.skill_timeline.is_editing():
            self.skill_timeline.exit_editing(commit=False)
            self._status("Edit cancelled.")
        self._update_escape()

    def _snap_ms(self, pos_ms: int):
        """(frame index, frame start seconds, ms to seek to show that frame) for a player position on
        the time source. Times are snapped to the start of the frame on screen, so time and frame
        number agree; never past the last video frame (audio may run longer than the video). The
        player itself is held mid-frame (see VideoGrid._park_ms)."""
        ts = self.video_grid.time_source() or ""
        fps = self.video_grid.fps_for(ts)
        frame = min(frame_at_ms(pos_ms, fps), self.video_grid.last_frame(ts))
        return frame, float(frame / fps), frame_centre_ms(frame, fps)

    def _cancel_step(self) -> None:
        """Abandon the label being recorded (nothing has been saved for it yet)."""
        if not self.state.is_capturing:
            return
        self._reset_capture()
        self._status("Label cancelled.")
        self._update_transport_buttons()

    def _start_step(self) -> None:
        if not self.state.is_loaded():
            QMessageBox.warning(self, "No session", "Open or create a session first.")
            return
        if not self.video_grid.is_ready():
            return
        if not self._skill_selected:
            QMessageBox.warning(self, "No label selected", "Select a label from the Labels panel.")
            return
        if not self._time_source_rate_known():
            return
        if self.skill_timeline.is_editing():
            QMessageBox.information(self, "Editing active", "Finish editing the timeline block first.")
            return
        self._capture_was_playing = self.video_grid.is_playing()
        self.video_grid.pause()
        step = self._skill_selected
        self.state.pending_step = step
        self.state.pending_camid = encode_camid(self.video_grid.active_views())
        self.state.pending_color = self._color(step.number)
        self.current_step_label.setText(
            f"Current step: {step.number} - {step.name}  (move to the first frame of the step, then Confirm Start)")
        self._set_capture_buttons(confirm_start=True)
        self.skill_timeline.set_allow_edit(False)
        self._update_transport_buttons()

    def _time_source_rate_known(self, video_id: Optional[str] = None) -> bool:
        """False (after telling the user) if the time source's frame rate is unknown: frame numbers
        would be computed at an assumed rate."""
        item = self.state.get_video(video_id or self.video_grid.time_source() or "")
        if item is None or item.rate_known:
            return True
        QMessageBox.warning(self, "Frame rate unknown",
                            f"The frame rate of {item.video_id} is unknown (it could not be measured when the session "
                            "was opened; see Session Info), so label frame numbers would be wrong. Make sure ffmpeg "
                            "(ffprobe) is installed and the video file is readable, then open the session again.")
        return False

    def _confirm_start(self) -> None:
        if not self.state.pending_step or not self._time_source_rate_known():
            return
        frame, start_s, seek_ms = self._snap_ms(self.video_grid.position())
        self.video_grid.seek(seek_ms)
        self.state.pending_start_ms = seek_ms
        self._pending_start_s = start_s
        self.state.pending_time_source = self.video_grid.time_source()
        self.state.pending_audio_source = self.video_grid.audio_source()
        step = self.state.pending_step
        self.current_step_label.setText(
            f"Current step: {step.number} - {step.name} | start {_precise(start_s)} (frame {frame}). "
            "Play to the last frame of the step, then End.")
        self._set_capture_buttons(end=True)
        if self._capture_was_playing:
            self.video_grid.play()
        self._refresh_pending_overlay(seek_ms)
        self._update_transport_buttons()

    def _end_step(self) -> None:
        if self.state.pending_start_ms is None or not self.state.pending_step:
            QMessageBox.warning(self, "No start", "Confirm Start before ending the step.")
            return
        self.video_grid.pause()
        step = self.state.pending_step
        self.current_step_label.setText(
            f"Current step: {step.number} - {step.name} | adjust the end with the slider or ←/→ if needed, "
            "then Confirm End.")
        self._set_capture_buttons(finish=True)
        self._update_transport_buttons()

    def _finish_step(self) -> None:
        st = self.state
        if st.pending_start_ms is None or not st.pending_step or self._pending_start_s is None:
            QMessageBox.warning(self, "Incomplete", "Step state incomplete; please start again.")
            return
        if self.video_grid.is_playing():
            self.video_grid.pause()
        end_frame, end_s, end_seek = self._snap_ms(self.video_grid.position())
        start_frame, _, _ = self._snap_ms(st.pending_start_ms)
        if end_frame < start_frame:
            QMessageBox.warning(self, "Invalid range", "The end is before the start. Move to the last frame of the step.")
            return
        if end_frame == start_frame:
            resp = QMessageBox.question(self, "Single frame", "Start and end are the same frame. Save a one-frame label?",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return
        self.video_grid.seek(end_seek)
        span = f"{_precise(self._pending_start_s)} → {_precise(end_s)}  (frames {start_frame}–{end_frame})"
        answer = ask_confidence_and_notes(self, st.pending_step, span)
        if answer is None:
            return
        confidence, notes = answer
        fps = self.video_grid.fps_for(st.pending_time_source or "")
        rec = recompute_from_times(AnnotationRecord(
            label=str(st.label), camid=str(st.pending_camid or ""), step_no=int(st.pending_step.number),
            step_name=str(st.pending_step.name), start_frame=0, end_frame=0, total_frames=0,
            start_time=self._pending_start_s, end_time=end_s, total_time=0.0,
            time_source=str(st.pending_time_source), audio_source=str(st.pending_audio_source),
            confidence=int(confidence), notes=notes,
        ), fps)
        self._remember(f"New Step {rec.step_no}")
        st.annotations.append(rec)
        resume = self._capture_was_playing
        self._reset_capture()
        self.current_step_label.setText("Current step: None (label saved)")
        self._refresh_annotations_view()
        self._save()
        if resume:
            self.video_grid.play()
        self._update_transport_buttons()

    def _refresh_pending_overlay(self, pos_ms: int) -> None:
        if self.state.pending_start_ms is None or self.state.pending_step is None:
            return
        start = int(self.state.pending_start_ms)
        color = self.state.pending_color or self._color(self.state.pending_step.number)
        self.slider.set_overlays([OverlayRange(start, max(start, int(pos_ms)), color, 140)])

    # =====================================================================
    # Editing and deleting saved annotations
    # =====================================================================

    def _index_of(self, rec: AnnotationRecord) -> int:
        return next((i for i, r in enumerate(self.state.annotations) if r is rec), -1)

    def _remember(self, what: str) -> None:
        """Keep the annotations as they are before a change, for Undo. (Records are never changed in
        place, only replaced, so a list of the same objects is a snapshot.)"""
        self._undo.append((what, list(self.state.annotations)))
        del self._undo[:-UNDO_DEPTH]
        self._redo.clear()
        self._update_undo_actions()

    def _undo_change(self) -> None:
        self._swap_history(self._undo, self._redo, "Undone")

    def _redo_change(self) -> None:
        self._swap_history(self._redo, self._undo, "Redone")

    def _swap_history(self, source: list, target: list, verb: str) -> None:
        if not source or not self.state.is_loaded():
            return
        if self.state.is_capturing or self.skill_timeline.is_editing():
            self._status("Finish or cancel the current label or edit first.")
            return
        what, records = source.pop()
        target.append((what, list(self.state.annotations)))
        self.state.annotations = records
        self.slider.clear_edit_overlay()
        self._refresh_annotations_view()
        self._update_undo_actions()
        if self._save(quiet=True):
            self._status(f"{verb}: {what}")

    def _update_undo_actions(self) -> None:
        loaded = self.state.is_loaded()
        self.act_undo.setEnabled(loaded and bool(self._undo))
        self.act_redo.setEnabled(loaded and bool(self._redo))
        self.act_undo.setText(f"Undo {self._undo[-1][0]}" if self._undo else "Undo")
        self.act_redo.setText(f"Redo {self._redo[-1][0]}" if self._redo else "Redo")

    def _apply_record_edit(self, old: AnnotationRecord, new: AnnotationRecord) -> None:
        """Validate an edited copy, snap its times to frames, and replace the original record."""
        idx = self._index_of(old)
        if idx < 0:
            self._refresh_annotations_view()
            return
        if (new.start_time, new.end_time) != (old.start_time, old.end_time):
            ts = self._loaded_video_id(new.time_source)
            if ts is None:
                QMessageBox.warning(self, "Invalid edit",
                                    f"This label's time source '{new.time_source}' is not a video of this session, "
                                    "so its times cannot be matched to frames.")
                self._refresh_annotations_view()
                return
            if not self._time_source_rate_known(ts):
                self._refresh_annotations_view()
                return
            fps = self.video_grid.fps_for(ts)
            times = [new.start_time, new.end_time]
            if not all(math.isfinite(t) and t >= 0 for t in times):
                QMessageBox.warning(self, "Invalid edit", "Times must be finite, non-negative numbers.")
                self._refresh_annotations_view()
                return
            last = self.video_grid.last_frame(ts)

            def snap(t: float, before: float, other: float) -> float:
                # a changed time goes to the frame shown at it (typed/stored times have 3 decimals:
                # displayed_frame() allows for that rounding); an unchanged one keeps the frame label.tsv
                # gives it (labels made by 0.1.x hold times between frames), and so does one moved onto
                # the other edge's time (a one-frame label on that edge's frame)
                frame = seconds_to_frames(t, fps) if t in (before, other) else displayed_frame(t, fps)
                return float(min(frame, last) / fps)

            start_s = snap(new.start_time, old.start_time, old.end_time)
            end_s = snap(new.end_time, old.end_time, old.start_time)
            if end_s < start_s:
                QMessageBox.warning(self, "Invalid edit", "The end must not be before the start.")
                self._refresh_annotations_view()
                return
            new = recompute_from_times(dataclasses.replace(new, start_time=start_s, end_time=end_s), fps)
        if self.skill_timeline.editing_record() is old:
            self.skill_timeline.exit_editing(commit=False)
        if new == old:  # nothing changed (e.g. a timeline edit saved without moving a handle)
            self._refresh_annotations_view()
            return
        self._remember(f"Edit of Step {old.step_no}")
        self.state.annotations[idx] = new
        self._refresh_annotations_view()
        self.table.select_record(new)
        self._save()

    def _delete_record(self, rec: AnnotationRecord) -> None:
        idx = self._index_of(rec)
        if idx < 0:
            return
        undo = QKeySequence(QKeySequence.Undo).toString(QKeySequence.NativeText)
        resp = QMessageBox.question(
            self, "Delete annotation",
            f"Delete this annotation?\n\nStep {rec.step_no}: {rec.step_name}\n"
            f"Start: {rec.start_time:.3f} s   End: {rec.end_time:.3f} s\n\n(Edit ▸ Undo, {undo}, brings it back.)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        if self.skill_timeline.editing_record() is rec:
            self.skill_timeline.exit_editing(commit=False)
        self._remember(f"Delete of Step {rec.step_no}")
        del self.state.annotations[idx]
        self.slider.clear_edit_overlay()
        self._refresh_annotations_view()
        self._save()

    def _refresh_annotations_view(self) -> None:
        self.table.set_records(self.state.annotations)
        self.skill_timeline.set_annotations(self.state.annotations)
        self.skill_timeline.set_allow_edit(not self.state.is_capturing)
        self._update_undo_actions()

    def _on_timeline_record_clicked(self, rec: Optional[AnnotationRecord]) -> None:
        if rec is None:  # a click on empty timeline: nothing is selected (Delete must not act on a stale row)
            self.table.clearSelection()
            return
        self.table.select_record(rec)
        self._show_record(rec)

    def _show_record(self, rec: AnnotationRecord) -> None:
        """Show a label's first frame (not while a label is being recorded: the frame on screen is
        what Confirm Start/End would take)."""
        self.skill_timeline.set_selected(rec)
        if self.state.is_capturing or self.skill_timeline.is_editing():
            return
        self._show_edge(rec, "start")

    def _go_to_edge(self, edge: str) -> None:
        """Show the first or last frame of the selected annotation."""
        rec = self.table.selected_record()
        if rec is None:
            self._status("Select an annotation first (click it on the timeline or in the table).")
        elif not self.state.is_capturing and not self.skill_timeline.is_editing():
            self._show_edge(rec, edge)

    def _set_edge_here(self, rec: Optional[AnnotationRecord], edge: str) -> None:
        """Make the frame on screen the selected annotation's first or last frame (the exact way to correct
        a boundary: step to the frame with ←/→, then set it)."""
        if rec is None:
            self._status("Select an annotation first (click it on the timeline or in the table).")
            return
        if self.state.is_capturing or self.skill_timeline.is_editing() or not self.video_grid.is_ready():
            self._status("Finish or cancel the current label or edit first.")
            return
        ts = self._loaded_video_id(rec.time_source)
        if ts is None or not self._time_source_rate_known(ts):
            if ts is None:
                QMessageBox.warning(self, "Invalid edit", f"This label's time source '{rec.time_source}' is not a "
                                                          "video of this session, so its times cannot be set from it.")
            return
        k = self.video_grid.frame_at(self.video_grid.position())  # the time source's frame on screen
        grid_fps, fps = self.video_grid.fps_for(self.video_grid.time_source() or ""), self.video_grid.fps_for(ts)
        if ts != self.video_grid.time_source():  # the label's own time source frame shown at this moment
            k = displayed_frame(round(float(k / grid_fps), 3), fps)  # (as a dragged edge maps it)
        other = seconds_to_frames(rec.end_time if edge == "start" else rec.start_time, fps)  # as label.tsv has it
        if (edge == "start" and k > other) or (edge == "end" and k < other):
            QMessageBox.warning(self, "Invalid edit", f"The {edge} must not be "
                                                      f"{'after the end' if edge == 'start' else 'before the start'}.")
            return
        new = dataclasses.replace(rec, **{f"{edge}_time": float(k / fps)})
        self._apply_record_edit(rec, new)
        self._status(f"Step {rec.step_no}: {edge} set to the frame on screen.")

    def _delete_selected(self) -> None:
        if self.state.is_capturing or self.skill_timeline.is_editing():
            return
        rec = self.table.selected_record()
        if rec is not None:
            self._delete_record(rec)

    def _on_timeline_request_edit(self, rec: AnnotationRecord) -> None:
        if self.state.is_capturing:
            QMessageBox.information(self, "Edit disabled", "Finish the current label before editing another.")
            self.skill_timeline.exit_editing(commit=False)
        else:
            self._status("Drag the white handles to change the start or end; click outside the block to save "
                         "(Esc cancels).", 0)
        self._update_escape()

    def _on_timeline_edit_preview(self, rec: AnnotationRecord, start_ms: int, end_ms: int, edge: str) -> None:
        self.slider.set_edit_overlay(start_ms, end_ms, color_hex=self._color(rec.step_no), alpha=180)
        if not self._timeline_dragging:
            return
        ms, stored = (start_ms, rec.start_time) if edge == "left" else (end_ms, rec.end_time)
        other_ms, other = (end_ms, rec.end_time) if edge == "left" else (start_ms, rec.start_time)
        if ms != int(round(stored * 1000.0)) and ms == other_ms == int(round(other * 1000.0)):
            stored = other  # resting on the other edge, which did not move: the label would become its frame
        ts = self._loaded_video_id(rec.time_source)
        if ts is not None and ms == int(round(stored * 1000.0)):  # on a stored edge: the frame label.tsv gives it
            ms = self._stored_edge_ms(ts, stored)
        self.video_grid.seek(ms)  # the frame under the handle

    def _stored_edge_ms(self, ts: str, stored: float) -> int:
        """Where the grid shows the frame label.tsv gives a stored edge time of a label whose time source is
        ``ts``. (Stored times have 3 decimals: taken as milliseconds they can lie in the previous frame,
        e.g. frame 61 at 30 fps is stored as 2.033 s, and 2033 ms is in frame 60.)"""
        fps = self.video_grid.fps_for(ts)
        j = seconds_to_frames(stored, fps)  # as label.tsv numbers it
        grid_fps = self.video_grid.fps_for(self.video_grid.time_source() or "")
        k = displayed_frame(round(float(j / fps), 3), grid_fps)
        if self.video_grid.time_source() != ts and self.video_grid.mapped_frame(ts, k) < j:
            k += 1  # the grid frame holding the edge's start shows the label's previous frame
        return frame_centre_ms(k, grid_fps)

    def _show_edge(self, rec: AnnotationRecord, edge: str) -> None:
        """Show a label's first ("start") or last ("end") frame, as label.tsv numbers it."""
        stored = rec.start_time if edge == "start" else rec.end_time
        ts = self._loaded_video_id(rec.time_source)
        self._jump_to(self._stored_edge_ms(ts, stored) if ts is not None else int(round(stored * 1000.0)))

    def _on_timeline_edit_canceled(self, _rec: AnnotationRecord) -> None:
        self.slider.clear_edit_overlay()
        self.statusBar().clearMessage()
        self._update_escape()

    def _on_timeline_drag_started(self) -> None:
        self._timeline_dragging = True
        self._drag_was_playing = self.video_grid.is_playing()
        if self._drag_was_playing:
            self.video_grid.pause()

    def _on_timeline_drag_finished(self) -> None:
        self._timeline_dragging = False
        if self._drag_was_playing:
            self.video_grid.play()
        self._drag_was_playing = False
        self._update_transport_buttons()

    def _on_timeline_edit_committed(self, rec: AnnotationRecord, start_ms: int, end_ms: int) -> None:
        self.slider.clear_edit_overlay()
        self.statusBar().clearMessage()
        self._update_escape()
        ts = self._loaded_video_id(rec.time_source)
        fps = self.video_grid.fps_for(ts or "")
        grid_ts = self.video_grid.time_source() or ""
        grid_fps = self.video_grid.fps_for(grid_ts)

        def edge(ms: int, stored: float) -> float:
            # a dragged edge becomes the start of the label's time-source frame that was shown under the
            # handle: the grid showed its own time source's frame at ms, and every other video the frame
            # mapped from that moment; an edge that was not moved keeps its stored time
            if ts is None or ms == int(round(stored * 1000.0)):
                return ms / 1000.0 if ts is None else stored
            k = self.video_grid.frame_at(ms)
            if ts == grid_ts:
                return float(k / fps)
            return float(displayed_frame(round(float(k / grid_fps), 3), fps) / fps)

        stored_start, stored_end = int(round(rec.start_time * 1000.0)), int(round(rec.end_time * 1000.0))
        # a handle dragged onto the other one, which did not move (the canvas stops it there), takes that
        # edge's time, so the label becomes the one frame label.tsv gives that edge
        onto_end = start_ms != stored_start and start_ms == end_ms == stored_end
        onto_start = end_ms != stored_end and end_ms == start_ms == stored_start
        start = rec.end_time if onto_end else edge(start_ms, rec.start_time)
        end = rec.start_time if onto_start else edge(end_ms, rec.end_time)
        if ts is not None and seconds_to_frames(end, fps) < seconds_to_frames(start, fps):
            # a handle dragged onto the other edge stops there (its millisecond can lie in the frame before)
            if end_ms != int(round(rec.end_time * 1000.0)):
                end = start
            else:
                start = end
        start, end = sorted((start, end))
        self._apply_record_edit(rec, dataclasses.replace(rec, start_time=start, end_time=end))

    def _loaded_video_id(self, video_id: str) -> Optional[str]:
        """The loaded video a stored id refers to (matched case-insensitively, like everywhere else)."""
        item = self.state.get_video(video_id or "")
        return item.video_id if item is not None and item.video_id in self.video_grid.video_ids() else None

    # =====================================================================
    # Export
    # =====================================================================

    def _export_clips(self) -> None:
        if not self.state.is_loaded():
            return
        if not self.state.annotations:
            QMessageBox.information(self, "Nothing to export", "This session has no annotations yet.")
            return
        if not self._save():
            return
        if self._recovery_dir is not None:
            QMessageBox.warning(self, "Cannot export",
                                f"This session's labels are being saved to {self._recovery_dir} instead of its folder "
                                "(see the earlier message, or the README, Troubleshooting), so it cannot be exported "
                                "from this window.")
            return
        dlg = ExportDialog(os.path.join(self.root_dir or "", "exports"), len(self.state.annotations), self)
        if dlg.exec_() != dlg.Accepted:
            return
        options = dlg.options()
        left_out = left_out_outputs(self.state, options)
        if left_out:  # e.g. MP4 clips unchecked this time: the earlier clips would be removed
            parts = [f"{n} {'MP4 clip' if kind == 'video' else 'WAV file'}{'s' if n != 1 else ''}"
                     for kind, n in sorted(left_out.items())]
            if QMessageBox.question(
                    self, "Remove earlier files?",
                    f"The output folder holds an earlier export of this session with {' and '.join(parts)} that "
                    "these options leave out. An export keeps its folder matching its manifest, so those files "
                    "will be removed.\n\nExport and remove them?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        state = self.state
        outcome = run_with_progress(
            self, "Exporting clips", "Preparing…",
            lambda report, cancelled: export_segments(state, options, report, cancelled))
        if outcome.status == "cancelled":
            self._status("Export cancelled.")
            return
        if not outcome.ok:
            head, details = split_error(outcome.error)
            show_error(self, "Export failed", head, details)
            return
        rep = outcome.value
        if not rep.results:  # every output was skipped: nothing was written or changed
            QMessageBox.warning(self, "Nothing exported",
                                "No file was written; an earlier export in that folder is unchanged.\n\n"
                                + "\n".join(f"• {w}" for w in rep.warnings[:10]))
            return
        ok = len(rep.results) - len(rep.failed)
        lines = [f"{ok} of {len(rep.results)} file(s) written.", f"Manifest: {rep.manifest_csv}"]
        lines += [f"FAILED {os.path.basename(r.job.out_path)}: {r.error}" for r in rep.failed[:10]]
        lines += [f"• {w}" for w in rep.warnings[:10]]
        box = QMessageBox(QMessageBox.Information if not rep.failed else QMessageBox.Warning,
                          "Export finished", "\n".join(lines), QMessageBox.Ok, self)
        open_btn = box.addButton("Open Folder", QMessageBox.ActionRole)
        box.exec_()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(rep.manifest_csv)))

    # =====================================================================
    # Misc
    # =====================================================================

    def _update_enabled_state(self) -> None:
        has_root = bool(self.root_dir)
        loaded = self.state.is_loaded()
        for w in (self.btn_create, self.btn_import, self.act_new, self.act_open):
            w.setEnabled(has_root)
        for w in (self.btn_info, self.combo_views, self.btn_delete_record, self.btn_export, self.btn_finish_session,
                  self.act_info, self.act_export, self.act_finish):
            w.setEnabled(loaded)
        self._update_source_combos()
        self._update_undo_actions()
        if loaded and not self.state.is_capturing:
            self._set_capture_buttons(start=self.video_grid.is_ready())
        elif not loaded:
            self._set_capture_buttons()
        self._update_transport_buttons()

    def closeEvent(self, event):  # noqa: N802 (Qt API)
        self._save_layout()
        if self.cfg and self.root_dir:
            self.cfg.skills = self.skills_panel.skills()
            self._save_config()
        if self.state.is_loaded():
            if self.state.is_capturing:
                resp = QMessageBox.question(self, "Unfinished label",
                                            "A label is still being recorded and will be lost. Quit anyway?",
                                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if resp != QMessageBox.Yes:
                    event.ignore()
                    return
            if not self._save(quiet=True):
                resp = QMessageBox.question(self, "Save failed", "Quit anyway and lose unsaved changes?",
                                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if resp != QMessageBox.Yes:
                    event.ignore()
                    return
            self.video_grid.clear()
        super().closeEvent(event)
