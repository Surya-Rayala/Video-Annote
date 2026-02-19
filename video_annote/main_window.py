# video_annote/main_window.py
from __future__ import annotations

import os
import shutil
from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .domain import (
    AnnotationRecord,
    RootConfig,
    SessionState,
    SkillStep,
    VideoItem,
    encode_camid_from_active_views,
    get_skill_color_hex,
)
from .persistence import (
    load_root_config,
    load_session_state,
    persist_session_state,
    save_root_config,
    session_exists,
    session_dir,
)
from .media_import import (
    import_local_video_into_session,
    import_url_video_into_session,
    safe_remove,
)
from .timeutils import ms_to_time_str, recompute_from_times
from .widgets.range_slider import RangeOverlaySlider, OverlayRange
from .widgets.video_grid import VideoGrid
from .widgets.skills_panel import SkillsPanel
from .widgets.annotations_table import AnnotationsTable
from .widgets.skill_timeline import SkillTimeline
from .widgets.checkable_combo import CheckableComboBox
from .dialogs.create_session import CreateSessionDialog, PendingVideo
from .dialogs.import_session import ImportSessionDialog


class MainWindow(QMainWindow):
    def __init__(self, root_dir: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Video-Annote (Multi-Video Annotation Tool)")
        self.resize(1800, 1000)

        # Root config + session state
        self.root_dir: Optional[str] = None
        self.cfg: Optional[RootConfig] = None
        self.state: SessionState = SessionState()

        # Step workflow state
        self._skill_selected: Optional[SkillStep] = None

        # Slider update guard
        self._ignore_slider_updates = False

        # Warn once per loaded session about skill mismatches (old sessions vs updated config)
        self._warned_skill_mismatch_label: Optional[str] = None

        # Scrub (drag slider) behavior: pause while dragging, resume only if it was playing.
        self._user_scrubbing: bool = False
        self._scrub_was_playing: bool = False

        # Timeline edit dragging: pause while dragging annotation handles, resume after drag ends
        # only if it was playing when the drag started.
        self._timeline_drag_active: bool = False
        self._timeline_drag_was_playing: bool = False

        self._timeline_drag_end_timer = QTimer(self)
        self._timeline_drag_end_timer.setSingleShot(True)
        self._timeline_drag_end_timer.setInterval(160)  # ms debounce for "drag ended"
        self._timeline_drag_end_timer.timeout.connect(self._on_timeline_drag_end)

        # Step workflow playback intent: remember if user was playing when they started a step.
        self._step_flow_was_playing: bool = False

        self._build_ui()
        self.set_root_dir(root_dir)

    # ---------------- UI ----------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # ===== Top: root + create/import + session label =====
        top = QHBoxLayout()
        top.setSpacing(10)
        main_layout.addLayout(top)

        root_box = QGroupBox("Data Root")
        root_lay = QHBoxLayout(root_box)
        root_lay.setContentsMargins(6, 6, 6, 6)
        self.root_label = QLabel("Not set")
        self.btn_set_root = QPushButton("Select Root")
        self.btn_set_root.clicked.connect(self._choose_root_dir)
        root_lay.addWidget(self.root_label, stretch=1)
        root_lay.addWidget(self.btn_set_root)
        top.addWidget(root_box, stretch=3)

        sess_box = QGroupBox("Session")
        sess_lay = QHBoxLayout(sess_box)
        sess_lay.setContentsMargins(6, 6, 6, 6)
        self.btn_create = QPushButton("Create New Session")
        self.btn_import = QPushButton("Import Existing Session")
        self.btn_create.clicked.connect(self._create_new_session_dialog)
        self.btn_import.clicked.connect(self._import_existing_session_dialog)

        self.session_label = QLabel("No session loaded")
        self.session_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        sess_lay.addWidget(self.btn_create)
        sess_lay.addWidget(self.btn_import)
        sess_lay.addStretch()
        sess_lay.addWidget(self.session_label)
        top.addWidget(sess_box, stretch=5)

        # ===== Middle: videos (left) + skills panel (right) =====
        split = QSplitter(Qt.Horizontal)
        main_layout.addWidget(split, stretch=12)

        # Left side container
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        # Row: camera views (checkable dropdown) + time/audio sources
        view_bar = QHBoxLayout()
        view_bar.setSpacing(8)

        self.combo_views = CheckableComboBox()
        self.combo_views.setMinimumWidth(220)
        self.combo_views.checked_ids_changed.connect(self._on_active_views_changed)
        self.combo_views.empty_selection_attempted.connect(self._warn_need_one_view)
        self.combo_views.dropdown_closed.connect(self._ensure_one_view_on_close)

        self.combo_time_source = QComboBox()
        self.combo_audio_source = QComboBox()
        self.combo_time_source.currentTextChanged.connect(self._on_time_source_changed)
        self.combo_audio_source.currentTextChanged.connect(self._on_audio_source_changed)

        self.view_label = QLabel("Selected (0) view")
        self.view_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        view_bar.addWidget(QLabel("Camera Views:"))
        view_bar.addWidget(self.combo_views)
        view_bar.addSpacing(12)
        view_bar.addWidget(QLabel("Time Source:"))
        view_bar.addWidget(self.combo_time_source)
        view_bar.addSpacing(12)
        view_bar.addWidget(QLabel("Audio Source:"))
        view_bar.addWidget(self.combo_audio_source)
        view_bar.addStretch()
        view_bar.addWidget(self.view_label)

        left_lay.addLayout(view_bar)

        # Video grid
        self.video_grid = VideoGrid()
        self.video_grid.time_position_changed.connect(self._on_master_position)
        self.video_grid.time_duration_changed.connect(self._on_master_duration)
        self.video_grid.time_source_ended.connect(self._on_time_source_ended)
        left_lay.addWidget(self.video_grid, stretch=12)

        # Playback controls
        play_bar = QHBoxLayout()
        play_bar.setSpacing(8)
        self.btn_play = QPushButton("Play")
        self.btn_pause = QPushButton("Pause")
        self.btn_play.clicked.connect(self._play)
        self.btn_pause.clicked.connect(self._pause)
        self.btn_restart = QPushButton("Restart")
        self.btn_restart.clicked.connect(self._restart)
        self.timeline_label = QLabel("00:00 / 00:00")
        play_bar.addWidget(self.btn_play)
        play_bar.addWidget(self.btn_pause)
        play_bar.addWidget(self.btn_restart)
        play_bar.addSpacing(12)
        play_bar.addWidget(self.timeline_label)
        play_bar.addStretch()
        left_lay.addLayout(play_bar)

        # Main slider
        self.slider = RangeOverlaySlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        # Scrub handling: pause during drag, resume on release if previously playing.
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        left_lay.addWidget(self.slider)

        # Timeline section under slider
        skills_timeline_box = QGroupBox("Timeline")
        st_lay = QVBoxLayout(skills_timeline_box)
        st_lay.setContentsMargins(6, 6, 6, 6)
        st_lay.setSpacing(6)

        self.skill_timeline = SkillTimeline()
        self.skill_timeline.request_edit.connect(self._on_skill_timeline_request_edit)
        self.skill_timeline.edit_preview.connect(self._on_skill_timeline_edit_preview)
        self.skill_timeline.edit_preview_drag.connect(self._on_skill_timeline_edit_preview_drag)
        self.skill_timeline.edit_committed.connect(self._on_skill_timeline_edit_commit)
        self.skill_timeline.edit_canceled.connect(self._on_skill_timeline_edit_cancel)
        self.skill_timeline.request_delete.connect(self._on_skill_timeline_request_delete)
        st_lay.addWidget(self.skill_timeline, stretch=1)

        left_lay.addWidget(skills_timeline_box, stretch=4)

        # Step buttons
        step_bar = QHBoxLayout()
        step_bar.setSpacing(8)
        self.btn_start_step = QPushButton("Start")
        self.btn_confirm_start = QPushButton("Confirm Start")
        self.btn_end_step = QPushButton("End")
        self.btn_finish_step = QPushButton("Confirm End")

        self.btn_confirm_start.setEnabled(False)
        self.btn_end_step.setEnabled(False)
        self.btn_finish_step.setEnabled(False)

        self.btn_start_step.clicked.connect(self._start_step)
        self.btn_confirm_start.clicked.connect(self._confirm_start)
        self.btn_end_step.clicked.connect(self._end_step)
        self.btn_finish_step.clicked.connect(self._finish_step)

        step_bar.addWidget(self.btn_start_step)
        step_bar.addWidget(self.btn_confirm_start)
        step_bar.addWidget(self.btn_end_step)
        step_bar.addWidget(self.btn_finish_step)
        step_bar.addStretch()

        self.current_step_label = QLabel("Current step: None")
        self.current_step_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        left_lay.addLayout(step_bar)
        left_lay.addWidget(self.current_step_label)

        # Right side: skills panel
        self.skills_panel = SkillsPanel()
        self.skills_panel.skill_selected.connect(self._on_skill_selected)
        self.skills_panel.skills_changed.connect(self._on_skills_changed)

        split.addWidget(left)
        split.addWidget(self.skills_panel)
        split.setStretchFactor(0, 12)
        split.setStretchFactor(1, 3)

        # ===== Bottom: annotations table + actions =====
        bottom = QWidget()
        bottom_lay = QVBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        bottom_lay.setSpacing(6)

        self.table = AnnotationsTable()
        self.table.annotations_changed.connect(self._on_table_annotations_changed)
        self.table.request_scroll_to_row.connect(self._scroll_to_row)
        self.table.set_fps_provider(lambda vid: self.video_grid.fps_for(vid))
        bottom_lay.addWidget(self.table, stretch=1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_delete_record = QPushButton("Delete Selected Record")
        self.btn_delete_record.clicked.connect(self.table.delete_selected_record)

        self.btn_finish_session = QPushButton("Finish Session")
        self.btn_finish_session.clicked.connect(self._finish_session)

        actions.addWidget(self.btn_delete_record)
        actions.addStretch()
        actions.addWidget(self.btn_finish_session)
        bottom_lay.addLayout(actions)

        main_layout.addWidget(bottom, stretch=2)

        self._update_enabled_state()

        self._apply_clickable_cursors()

    def _apply_clickable_cursors(self) -> None:
        """Provide lightweight hover affordance for interactive widgets."""

        def hand(w):
            try:
                if w is not None:
                    w.setCursor(Qt.PointingHandCursor)
            except Exception:
                pass

        # Buttons
        for w in (
            getattr(self, "btn_set_root", None),
            getattr(self, "btn_create", None),
            getattr(self, "btn_import", None),
            getattr(self, "btn_play", None),
            getattr(self, "btn_pause", None),
            getattr(self, "btn_restart", None),
            getattr(self, "btn_start_step", None),
            getattr(self, "btn_confirm_start", None),
            getattr(self, "btn_end_step", None),
            getattr(self, "btn_finish_step", None),
            getattr(self, "btn_delete_record", None),
            getattr(self, "btn_finish_session", None),
        ):
            hand(w)

        # Dropdowns / selectors
        for w in (
            getattr(self, "combo_views", None),
            getattr(self, "combo_time_source", None),
            getattr(self, "combo_audio_source", None),
        ):
            hand(w)

        # Note: do NOT set a global cursor on the main slider here.
        # `RangeOverlaySlider` handles handle-only cursor feedback internally.

    # ---------------- Root config ----------------

    def _choose_root_dir(self):
        from PyQt5.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "Select Data Root")
        if d:
            self.set_root_dir(d)

    def set_root_dir(self, root_dir: Optional[str]) -> None:
        if root_dir:
            self.root_dir = root_dir
            # Root changed; allow mismatch warnings to show again for subsequently loaded sessions
            self._warned_skill_mismatch_label = None
            self.root_label.setText(root_dir)
            self.cfg = load_root_config(root_dir)
            if self.cfg is None:
                self.cfg = RootConfig(root_dir=root_dir, skills=[], skill_color_map={})
            self.skills_panel.set_config(self.cfg)
            self.skills_panel.set_skills(self.cfg.skills)
            self.table.set_skills(self.cfg.skills)
            self.skill_timeline.set_color_resolver(lambda step_no: get_skill_color_hex(step_no, self.cfg))
        else:
            self.root_dir = None
            self._warned_skill_mismatch_label = None
            self.root_label.setText("Not set")
            self.cfg = None
            self.skills_panel.set_config(None)
            self.skills_panel.set_skills([])
            self.table.set_skills([])
        self._update_enabled_state()

    def _on_skills_changed(self):
        if not self.cfg:
            return
        self.cfg.skills = self.skills_panel.skills()
        self.table.set_skills(self.cfg.skills)
        try:
            save_root_config(self.cfg)
        except Exception as e:
            QMessageBox.warning(self, "Config save failed", str(e))
        # Skills list changed; allow mismatch warnings to re-trigger on next session load
        self._warned_skill_mismatch_label = None
        self._refresh_views()

    # ---------------- Session dialogs ----------------

    def _create_new_session_dialog(self):
        if not self.root_dir:
            QMessageBox.warning(self, "No Root", "Please select a Data Root first.")
            return

        dlg = CreateSessionDialog(self)
        if dlg.exec_() != dlg.Accepted:
            return

        label = dlg.session_label()
        pending = dlg.pending_videos()

        exists, path = session_exists(self.root_dir, label)
        if exists:
            resp = QMessageBox.question(
                self,
                "Session exists",
                f"Session '{label}' already exists at:\n{path}\n\n"
                f"Delete this session folder to recreate?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
            safe_remove(path)

        self._commit_new_session(label, pending)

    def _commit_new_session(self, label: str, pending: List[PendingVideo]) -> None:
        assert self.root_dir is not None
        sdir = session_dir(self.root_dir, label)
        os.makedirs(sdir, exist_ok=True)

        videos: List[VideoItem] = []
        try:
            for pv in pending:
                if pv.source_type == "local":
                    info = import_local_video_into_session(sdir, pv.video_id, pv.source_value, copy_instead_of_move=True)
                else:
                    info = import_url_video_into_session(sdir, pv.video_id, pv.source_value, force_mp4_output=True)

                videos.append(
                    VideoItem(
                        video_id=info.video_id,
                        filename=info.filename,
                        source_type=info.source_type,
                        source=info.source,
                        duration_ms=int(info.duration_ms or 0),
                        fps=float(info.fps or 30.0),
                    )
                )
        except Exception as e:
            QMessageBox.critical(self, "Import failed", f"Failed importing videos:\n{e}")
            return

        # Allow mismatch warnings for this new session
        self._warned_skill_mismatch_label = None
        self.state = SessionState(root_dir=self.root_dir, label=label, session_dir=sdir)
        self.state.videos = videos
        self.state.annotations = []
        self.state.ensure_default_sources()

        try:
            persist_session_state(self.state)
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))

        self._load_session_into_ui()

    def _import_existing_session_dialog(self):
        if not self.root_dir:
            QMessageBox.warning(self, "No Root", "Please select a Data Root first.")
            return

        dlg = ImportSessionDialog(self.root_dir, self)
        if dlg.exec_() != dlg.Accepted:
            return
        label = dlg.selected_label()
        if not label:
            return

        # Allow mismatch warnings for this import even if this session was loaded earlier
        self._warned_skill_mismatch_label = None
        self.state = load_session_state(self.root_dir, label)
        self._load_session_into_ui()

    # ---------------- Load session into UI ----------------

    def _load_session_into_ui(self):
        self.session_label.setText(f"Session: {self.state.label}  ({self.state.session_dir})")

        self.video_grid.load_videos(self.state.session_dir or "", self.state.videos)

        self._populate_video_selectors()

        # time/audio sources from state
        if self.state.time_source_id:
            self.video_grid.set_time_source(self.state.time_source_id)
        if self.state.audio_source_id:
            self.video_grid.set_audio_source(self.state.audio_source_id)

        dur = self.video_grid.duration_for(self.video_grid.time_source() or "")
        self._set_slider_range(dur)
        self._set_slider_value(0)

        self._update_timeline_label(0, dur)

        self.table.set_records(self.state.annotations)
        self._refresh_overlays_and_timeline()

        # Warn if the imported session contains steps that are missing/renamed in current root config
        self._warn_if_session_skills_mismatch()

        self._reset_step_flow()

        self._update_enabled_state()
        self._update_play_pause_buttons()

    def _populate_video_selectors(self):
        ids = self.state.video_ids()

        self.combo_time_source.blockSignals(True)
        self.combo_audio_source.blockSignals(True)
        try:
            self.combo_time_source.clear()
            self.combo_audio_source.clear()
            for vid in ids:
                self.combo_time_source.addItem(vid)
                self.combo_audio_source.addItem(vid)
        finally:
            self.combo_time_source.blockSignals(False)
            self.combo_audio_source.blockSignals(False)

        # Populate camera view checklist
        self.combo_views.set_items(ids)

        # Decide checked views
        checked = [v for v in (self.state.active_view_ids or []) if v in ids]
        if not checked and ids:
            checked = [ids[0]]

        # Set checked without emitting (we'll sync manually right after)
        self.combo_views.set_checked_ids(checked, emit_signal=False)

        # Sync grid/state immediately
        self.video_grid.set_active_views(checked)
        self.state.active_view_ids = self.video_grid.active_views()

        # sync current time/audio selections
        if self.state.time_source_id in ids:
            self.combo_time_source.setCurrentText(self.state.time_source_id)
        elif ids:
            self.combo_time_source.setCurrentText(ids[0])

        if self.state.audio_source_id in ids:
            self.combo_audio_source.setCurrentText(self.state.audio_source_id)
        elif ids:
            self.combo_audio_source.setCurrentText(ids[0])

        self._update_view_label()

    def _update_play_pause_buttons(self) -> None:
        """Reflect current playback state in Play/Pause/Restart buttons."""
        has_session = self.state.is_loaded()
        if not has_session:
            self.btn_play.setEnabled(False)
            self.btn_pause.setEnabled(False)
            # Restart may not exist in older builds; guard just in case.
            if hasattr(self, "btn_restart"):
                self.btn_restart.setEnabled(False)
            return

        playing = bool(self.video_grid.is_playing())
        pos = int(self.slider.value())
        dur = int(self.slider.maximum())
        at_end = (dur > 0 and pos >= dur)

        if hasattr(self, "btn_restart"):
            self.btn_restart.setEnabled(True)

        if playing:
            self.btn_play.setEnabled(False)
            self.btn_pause.setEnabled(True)
        else:
            # If we're at the end, disable Play (prevents implicit restart).
            self.btn_play.setEnabled(not at_end)
            self.btn_pause.setEnabled(False)

    def _is_at_end(self) -> bool:
        """True if the timeline is at (or extremely close to) the end."""
        try:
            pos = int(self.slider.value())
            dur = int(self.slider.maximum())
        except Exception:
            return False
        # treat 'near end' as end to avoid backend rounding quirks (e.g. 1-2ms)
        return bool(dur > 0 and pos >= max(0, dur - 2))

    def _resume_if_allowed(self, was_playing: bool) -> None:
        """
        Resume playback only if:
          - user was playing before, AND
          - we are NOT at end-of-media
        This prevents QMediaPlayer from restarting at 0 when play() is called at EndOfMedia.
        """
        if not was_playing or self._is_at_end():
            self.video_grid.pause_all()
            self._update_play_pause_buttons()
            return

        self.video_grid.play_all()
        self._update_play_pause_buttons()
        QTimer.singleShot(250, self._update_play_pause_buttons)


    # ---------------- Playback + slider ----------------

    def _play(self):
        if not self.state.is_loaded():
            return

        # If we're at (or extremely near) the end, don't auto-restart.
        pos = int(self.slider.value())
        dur = int(self.slider.maximum())
        if dur > 0 and pos >= dur:
            # User should press Restart explicitly.
            self._update_play_pause_buttons()
            return

        self.video_grid.play_all()
        # Playback can be delayed by buffer gate; reflect intent immediately and re-check shortly.
        self._update_play_pause_buttons()
        QTimer.singleShot(250, self._update_play_pause_buttons)

    def _restart(self):
        if not self.state.is_loaded():
            return

        was_playing = bool(self.video_grid.is_playing())

        # Pause first to avoid end-of-media quirks and keep the restart clean.
        self.video_grid.pause_all()

        self._ignore_slider_updates = True
        try:
            self.slider.setValue(0)
            self.video_grid.set_position_all(0)
        finally:
            self._ignore_slider_updates = False

        dur = int(self.slider.maximum())
        self._update_timeline_label(0, dur)
        self.skill_timeline.set_playhead_ms(0)
        self._refresh_overlays_and_timeline()

        if was_playing:
            self.video_grid.play_all()
            self._update_play_pause_buttons()
            QTimer.singleShot(250, self._update_play_pause_buttons)
        else:
            self._update_play_pause_buttons()

    def _pause(self):
        self.video_grid.pause_all()
        self._update_play_pause_buttons()

    def _on_master_duration(self, dur_ms: int):
        self._set_slider_range(int(dur_ms))
        self.skill_timeline.set_duration_ms(int(dur_ms))
        self._refresh_overlays_and_timeline()

    def _on_master_position(self, pos_ms: int):
        if self._ignore_slider_updates or self._user_scrubbing:
            return
        self._set_slider_value(int(pos_ms))
        dur = self.slider.maximum()
        self._update_timeline_label(int(pos_ms), int(dur))
        self.skill_timeline.set_playhead_ms(int(pos_ms))
        self._refresh_overlays_and_timeline()
        self._update_play_pause_buttons()

    def _on_time_source_ended(self):
        end_pos = self.slider.maximum()
        self._set_slider_value(end_pos)
        self._update_timeline_label(end_pos, end_pos)
        self._update_play_pause_buttons()

    def _on_slider_pressed(self) -> None:
        if not self.state.is_loaded():
            return
        # Mark scrubbing so master position updates won't overwrite the user's drag.
        self._user_scrubbing = True
        # If we were playing, pause once for the whole scrub gesture.
        self._scrub_was_playing = bool(self.video_grid.is_playing())
        if self._scrub_was_playing:
            self.video_grid.pause_all()
        self._update_play_pause_buttons()

    def _on_slider_released(self) -> None:
        if not self.state.is_loaded():
            self._user_scrubbing = False
            self._scrub_was_playing = False
            self._update_play_pause_buttons()
            return

        # End of user scrub gesture.
        self._user_scrubbing = False

        # Seek to the final position (in case tracking is off / last move missed).
        pos = int(self.slider.value())
        self._ignore_slider_updates = True
        try:
            self.video_grid.set_position_all(pos)
        finally:
            self._ignore_slider_updates = False

        self._update_timeline_label(pos, int(self.slider.maximum()))
        self.skill_timeline.set_playhead_ms(pos)
        self._refresh_overlays_and_timeline()

        # Resume only if we were playing when the user started scrubbing.
        self._resume_if_allowed(self._scrub_was_playing)

        self._scrub_was_playing = False
        self._update_play_pause_buttons()

    def _on_slider_moved(self, pos):
        if not self.state.is_loaded():
            return

        p = int(max(0, min(int(pos), int(self.slider.maximum()))))

        # While scrubbing we have already paused (if needed) in _on_slider_pressed.
        # Keep seeking responsive without restarting playback on every mouse move.
        self._ignore_slider_updates = True
        try:
            self.video_grid.set_position_all(p)
        finally:
            self._ignore_slider_updates = False

        self._update_timeline_label(p, int(self.slider.maximum()))
        self.skill_timeline.set_playhead_ms(p)
        self._refresh_overlays_and_timeline()

    def _set_slider_range(self, dur_ms: int):
        self._ignore_slider_updates = True
        try:
            self.slider.setRange(0, max(0, int(dur_ms)))
        finally:
            self._ignore_slider_updates = False

    def _set_slider_value(self, ms: int):
        self._ignore_slider_updates = True
        try:
            self.slider.setValue(max(0, int(ms)))
        finally:
            self._ignore_slider_updates = False

    def _update_timeline_label(self, pos_ms: int, dur_ms: int):
        self.timeline_label.setText(f"{ms_to_time_str(pos_ms)} / {ms_to_time_str(dur_ms)}")

    # ---------------- View/time/audio selectors ----------------

    def _warn_need_one_view(self):
        QMessageBox.warning(self, "Selection required", "At least one Video must be selected.")

    def _ensure_one_view_on_close(self):
        if not self.state.is_loaded():
            return
        views = self.combo_views.checked_ids()
        if views:
            return
        ids = self.state.video_ids()
        fallback = [ids[0]] if ids else []
        if fallback:
            self.combo_views.set_checked_ids(fallback, emit_signal=False)
            self.video_grid.set_active_views(fallback)
            self.state.active_view_ids = self.video_grid.active_views()
            self._update_view_label()

    def _on_active_views_changed(self, views: List[str]) -> None:
        if not self.state.is_loaded():
            return

        views = [v for v in (views or []) if v in self.state.video_ids()]

        if not views:
            # revert to previous (or first)
            prev = [v for v in (self.state.active_view_ids or []) if v in self.state.video_ids()]
            if not prev:
                ids = self.state.video_ids()
                prev = [ids[0]] if ids else []

            QMessageBox.warning(self, "Selection required", "At least one Video must be selected.")
            self.combo_views.set_checked_ids(prev, emit_signal=False)
            return

        self.video_grid.set_active_views(views)
        self.state.active_view_ids = self.video_grid.active_views()
        self._update_view_label()

    def _on_time_source_changed(self, vid: str):
        if not self.state.is_loaded():
            return
        if not vid:
            return
        if self.state.pending_step is not None and self.state.pending_start_ms is not None:
            QMessageBox.information(self, "Time source locked", "Finish the current step before changing time source.")
            if self.state.pending_time_source:
                self.combo_time_source.blockSignals(True)
                try:
                    self.combo_time_source.setCurrentText(self.state.pending_time_source)
                finally:
                    self.combo_time_source.blockSignals(False)
            return

        self.video_grid.set_time_source(vid)
        self.state.time_source_id = vid

        dur = self.video_grid.duration_for(vid)
        self._set_slider_range(dur)

        pos = self.slider.value()
        self._set_slider_value(pos)
        self._update_timeline_label(pos, dur)

        self.skill_timeline.set_duration_ms(dur)
        self._refresh_overlays_and_timeline()

    def _on_audio_source_changed(self, vid: str):
        if not self.state.is_loaded():
            return
        if not vid:
            return
        self.video_grid.set_audio_source(vid)
        self.state.audio_source_id = vid
        self._refresh_overlays_and_timeline()

    def _update_view_label(self):
        n = len(self.video_grid.active_views() or [])
        if n <= 0:
            self.view_label.setText("Selected (0) view")
        elif n == 1:
            self.view_label.setText("Selected (1) view")
        else:
            self.view_label.setText(f"Selected ({n}) views")

    # ---------------- Skill selection ----------------

    def _on_skill_selected(self, step: Optional[SkillStep]):
        self._skill_selected = step

    # ---------------- Step workflow ----------------

    def _reset_step_flow(self):
        self.state.clear_pending()
        self.current_step_label.setText("Current step: None")
        self.btn_start_step.setEnabled(True)
        self.btn_confirm_start.setEnabled(False)
        self.btn_end_step.setEnabled(False)
        self.btn_finish_step.setEnabled(False)
        self.skill_timeline.set_allow_edit(True)
        self.slider.clear_edit_overlay()
        self._step_flow_was_playing = False

    def _start_step(self):
        if not self.state.is_loaded():
            QMessageBox.warning(self, "No session", "Create or import a session first.")
            return
        if not self._skill_selected:
            QMessageBox.warning(self, "No label selected", "Select a label from the Labels panel.")
            return
        if self.skill_timeline.is_editing():
            QMessageBox.information(self, "Editing active", "Exit timeline edit mode before starting a new label.")
            return

        self._step_flow_was_playing = bool(self.video_grid.is_playing())
        self.video_grid.pause_all()
        self._update_play_pause_buttons()

        step = self._skill_selected
        self.state.pending_step = step
        self.state.pending_camid = encode_camid_from_active_views(self.video_grid.active_views())
        self.state.pending_start_ms = None
        self.state.pending_time_source = None
        self.state.pending_audio_source = None
        self.state.pending_color = get_skill_color_hex(step.number, self.cfg)

        self.current_step_label.setText(
            f"Current step: {step.number} - {step.name} (Select start on timeline, then Confirm Start)"
        )

        self.btn_start_step.setEnabled(False)
        self.btn_confirm_start.setEnabled(True)
        self.btn_end_step.setEnabled(False)
        self.btn_finish_step.setEnabled(False)

        self.skill_timeline.set_allow_edit(False)
        self._refresh_overlays_and_timeline()

    def _confirm_start(self):
        if not self.state.pending_step:
            return
        time_source = self.video_grid.time_source()
        audio_source = self.video_grid.audio_source()
        if not time_source or not audio_source:
            QMessageBox.warning(self, "Missing sources", "Time source and audio source must be selected.")
            return

        start_ms = self.slider.value()
        self.state.pending_start_ms = int(start_ms)
        self.state.pending_time_source = time_source
        self.state.pending_audio_source = audio_source

        self.current_step_label.setText(
            f"Current step: {self.state.pending_step.number} - {self.state.pending_step.name} | "
            f"Start at {ms_to_time_str(start_ms)}. Play to end, then click End Step."
        )

        self.btn_confirm_start.setEnabled(False)
        self.btn_end_step.setEnabled(True)
        self.btn_finish_step.setEnabled(False)

        # If we're at the end, never resume playback (prevents implicit restart to 0).
        self._resume_if_allowed(self._step_flow_was_playing)    

        self._refresh_overlays_and_timeline()

    def _end_step(self):
        if self.state.pending_start_ms is None or not self.state.pending_step:
            QMessageBox.warning(self, "No start", "Confirm Start before ending the step.")
            return

        self.video_grid.pause_all()
        self._update_play_pause_buttons()

        self.current_step_label.setText(
            f"Current step: {self.state.pending_step.number} - {self.state.pending_step.name} | "
            f"Adjust end on timeline if needed, then Finish Step to save."
        )

        self.btn_end_step.setEnabled(False)
        self.btn_finish_step.setEnabled(True)
        self._refresh_overlays_and_timeline()

    def _finish_step(self):
        if (
            self.state.pending_start_ms is None
            or not self.state.pending_step
            or not self.state.pending_camid
            or not self.state.pending_time_source
            or not self.state.pending_audio_source
        ):
            QMessageBox.warning(self, "Incomplete", "Step state incomplete; try again.")
            return

        end_ms = int(self.slider.value())
        start_ms = int(self.state.pending_start_ms)

        if end_ms < start_ms:
            QMessageBox.warning(self, "Invalid range", "End time must be after start time.")
            return

        conf_notes = self._prompt_confidence_and_notes(self.state.pending_step)
        if conf_notes is None:
            return
        confidence, notes = conf_notes

        fps = self.video_grid.fps_for(self.state.pending_time_source)
        start_time = start_ms / 1000.0
        end_time = end_ms / 1000.0

        start_frame = int(round(start_time * fps))
        end_frame = int(round(end_time * fps))

        rec = AnnotationRecord(
            label=str(self.state.label),
            camid=str(self.state.pending_camid),
            step_no=int(self.state.pending_step.number),
            step_name=str(self.state.pending_step.name),
            start_frame=start_frame,
            end_frame=end_frame,
            total_frames=max(end_frame - start_frame, 0),
            start_time=float(start_time),
            end_time=float(end_time),
            total_time=float(end_time - start_time),
            time_source=str(self.state.pending_time_source),
            audio_source=str(self.state.pending_audio_source),
            confidence=int(confidence),
            notes=str(notes or ""),
        )

        self.state.annotations.append(rec)
        self.table.set_records(self.state.annotations)

        self._autosave_session()

        self.state.clear_pending()
        self.current_step_label.setText("Current step: None (record saved)")

        self.btn_start_step.setEnabled(True)
        self.btn_confirm_start.setEnabled(False)
        self.btn_end_step.setEnabled(False)
        self.btn_finish_step.setEnabled(False)

        self.skill_timeline.set_allow_edit(True)

        # If we're at the end, never resume playback (prevents implicit restart to 0).
        self._resume_if_allowed(self._step_flow_was_playing)

        # Reset intent for next workflow.
        self._step_flow_was_playing = False

        self._refresh_overlays_and_timeline()

    def _prompt_confidence_and_notes(self, step: SkillStep):
        from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QSlider, QTextEdit

        dlg = QDialog(self)
        dlg.setWindowTitle("Step Self-Assessment")
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)

        title = QLabel(f"Step {step.number}: {step.name}")
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(title)

        row = QHBoxLayout()
        row.addWidget(QLabel("Confidence (1-10):"))
        slider = QSlider(Qt.Horizontal)
        slider.setRange(1, 10)
        slider.setValue(5)
        slider.setTickInterval(1)
        slider.setTickPosition(QSlider.TicksBelow)

        val = QLabel(str(slider.value()))
        val.setMinimumWidth(24)
        val.setAlignment(Qt.AlignCenter)
        slider.valueChanged.connect(lambda v: val.setText(str(v)))

        row.addWidget(slider, stretch=1)
        row.addWidget(val)
        layout.addLayout(row)

        layout.addWidget(QLabel("Notes (optional):"))
        notes = QTextEdit()
        notes.setPlaceholderText("Add any additional context or explanation here...")
        notes.setFixedHeight(120)
        layout.addWidget(notes)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec_() == QDialog.Accepted:
            return (int(slider.value()), notes.toPlainText().strip())
        return None

    # ---------------- Autosave / refresh ----------------

    def _autosave_session(self):
        try:
            persist_session_state(self.state)
        except Exception as e:
            QMessageBox.warning(self, "Autosave failed", str(e))

    def _refresh_views(self):
        if self.cfg:
            self.cfg.skills = self.skills_panel.skills()
            self.table.set_skills(self.cfg.skills)

        self.state.annotations = self.table.records()

        self._refresh_overlays_and_timeline()

    def _refresh_overlays_and_timeline(self):
        self.skill_timeline.set_duration_ms(self.slider.maximum())
        self.skill_timeline.set_annotations(self.state.annotations)
        self.skill_timeline.set_playhead_ms(self.slider.value())

        overlays: List[OverlayRange] = []

        if self.state.pending_start_ms is not None and self.state.pending_step is not None:
            s = int(self.state.pending_start_ms)
            e = int(self.slider.value())
            if e < s:
                e = s
            color = self.state.pending_color or get_skill_color_hex(int(self.state.pending_step.number), self.cfg)
            overlays.append(OverlayRange(start_value=s, end_value=e, color_hex=color, alpha=140))

        self.slider.set_overlays(overlays)

        allow_edit = (self.state.pending_step is None and self.state.pending_start_ms is None)
        self.skill_timeline.set_allow_edit(allow_edit)

        self._update_view_label()

    # ---------------- Table -> state ----------------

    def _on_table_annotations_changed(self, records: List[AnnotationRecord]):
        self.state.annotations = list(records or [])
        self._autosave_session()
        self._refresh_overlays_and_timeline()

    def _scroll_to_row(self, row: int):
        if row < 0:
            return
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, 0))

    # ---------------- Timeline drag gating (pause while dragging, resume after) ----------------

    def _begin_timeline_drag(self) -> None:
        """Called on first handle-drag event for an annotation block."""
        if self._timeline_drag_active:
            return
        self._timeline_drag_active = True
        self._timeline_drag_was_playing = bool(self.video_grid.is_playing())
        if self._timeline_drag_was_playing:
            self.video_grid.pause_all()
        self._update_play_pause_buttons()

    def _on_timeline_drag_end(self) -> None:
        """Called after a short debounce when handle dragging stops."""
        if not self._timeline_drag_active:
            return
        self._timeline_drag_active = False

        if self._timeline_drag_was_playing:
            self.video_grid.play_all()
            self._update_play_pause_buttons()
            QTimer.singleShot(250, self._update_play_pause_buttons)

        self._timeline_drag_was_playing = False
        self._update_play_pause_buttons()

    # ---------------- Skill timeline edit integration ----------------

    def _on_skill_timeline_request_edit(self, idx: int):
        if self.state.pending_step is not None or self.state.pending_start_ms is not None:
            QMessageBox.information(self, "Edit disabled", "Finish the current step before editing an annotation.")
            self.skill_timeline.exit_editing(commit=False)
            return

    def _on_skill_timeline_request_delete(self, idx: int) -> None:
        """Delete an annotation record requested from the timeline UI."""
        if not self.state.is_loaded():
            return

        # Don't allow deletion while a step is being recorded.
        if self.state.pending_step is not None or self.state.pending_start_ms is not None:
            QMessageBox.information(self, "Delete disabled", "Finish the current step before deleting an annotation.")
            # Ensure any edit overlay is cleared.
            try:
                self.skill_timeline.exit_editing(commit=False)
            except Exception:
                pass
            self.slider.clear_edit_overlay()
            self._refresh_overlays_and_timeline()
            return

        if idx < 0 or idx >= len(self.state.annotations):
            return

        rec = self.state.annotations[idx]
        msg = (
            f"Delete this annotation?\n\n"
            f"Step {rec.step_no}: {rec.step_name}\n"
            f"Start: {rec.start_time:.3f}s\n"
            f"End:   {rec.end_time:.3f}s\n"
        )
        resp = QMessageBox.question(
            self,
            "Delete annotation",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            # If timeline initiated editing, clear any overlay but otherwise do nothing.
            self.slider.clear_edit_overlay()
            self._refresh_overlays_and_timeline()
            return

        # Exit editing mode if active so hit maps / overlays stay consistent.
        try:
            if self.skill_timeline.is_editing():
                self.skill_timeline.exit_editing(commit=False)
        except Exception:
            pass

        # Remove the record.
        del self.state.annotations[idx]

        # Refresh table + timeline.
        self.table.set_records(self.state.annotations)
        self.slider.clear_edit_overlay()
        self._autosave_session()
        self._refresh_overlays_and_timeline()

    def _on_skill_timeline_edit_preview(self, idx: int, start_ms: int, end_ms: int):
        if idx < 0 or idx >= len(self.state.annotations):
            return
        rec = self.state.annotations[idx]
        color = get_skill_color_hex(int(rec.step_no), self.cfg)
        self.slider.set_edit_overlay(int(start_ms), int(end_ms), color_hex=color, alpha=180)

    def _on_skill_timeline_edit_preview_drag(self, idx: int, start_ms: int, end_ms: int, edge: str):
        if idx < 0 or idx >= len(self.state.annotations):
            return
        rec = self.state.annotations[idx]
        color = get_skill_color_hex(int(rec.step_no), self.cfg)
        self.slider.set_edit_overlay(int(start_ms), int(end_ms), color_hex=color, alpha=180)

        # Pause once at the beginning of a drag gesture, and resume after dragging stops
        # only if playback was active at drag start.
        self._begin_timeline_drag()
        # Restart debounce timer on every drag update
        self._timeline_drag_end_timer.start()

        edge = (edge or "").lower().strip()
        target = int(start_ms) if edge == "left" else int(end_ms)
        target = max(0, min(target, int(self.slider.maximum())))

        # Seek while staying paused (set_position_all may resume if it thinks we were playing;
        # our pause gate ensures stable playback state during dragging).
        self._ignore_slider_updates = True
        try:
            self.slider.setValue(target)
            self.video_grid.set_position_all(target)
        finally:
            self._ignore_slider_updates = False

        self._update_timeline_label(target, int(self.slider.maximum()))
        self.skill_timeline.set_playhead_ms(target)

    def _on_skill_timeline_edit_commit(self, idx: int, start_ms: int, end_ms: int):
        # If a drag gate was active, end it now so playback can resume appropriately.
        self._on_timeline_drag_end()
        self.slider.clear_edit_overlay()
        if idx < 0 or idx >= len(self.state.annotations):
            return
        rec = self.state.annotations[idx]
        if end_ms < start_ms:
            start_ms, end_ms = end_ms, start_ms

        rec.start_time = start_ms / 1000.0
        rec.end_time = end_ms / 1000.0
        fps = self.video_grid.fps_for(rec.time_source)
        try:
            new_rec = recompute_from_times(rec, fps)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid edit", str(e))
            return

        rec.start_time = new_rec.start_time
        rec.end_time = new_rec.end_time
        rec.total_time = new_rec.total_time
        rec.start_frame = new_rec.start_frame
        rec.end_frame = new_rec.end_frame
        rec.total_frames = new_rec.total_frames

        self.table.set_records(self.state.annotations)
        self._autosave_session()
        self._refresh_overlays_and_timeline()

    def _on_skill_timeline_edit_cancel(self, idx: int):
        # If a drag gate was active, end it now so playback can resume appropriately.
        self._on_timeline_drag_end()
        self.slider.clear_edit_overlay()
        self._refresh_overlays_and_timeline()

    # ---------------- Finish session ----------------

    def _finish_session(self):
        if not self.state.is_loaded():
            return
        self._autosave_session()

        self.video_grid.clear()
        self.slider.setRange(0, 0)
        self.slider.setValue(0)
        self.slider.clear_overlays()
        self.slider.clear_edit_overlay()

        self.state = SessionState(root_dir=self.root_dir)
        self.session_label.setText("No session loaded")

        self.table.set_records([])
        self.skill_timeline.set_annotations([])
        self.skill_timeline.set_duration_ms(0)
        self.skill_timeline.set_playhead_ms(0)

        self._reset_step_flow()
        self._update_enabled_state()
        self._update_play_pause_buttons()

        QMessageBox.information(self, "Session finished", "Session finished and saved. You can create/import another session.")

    # ---------------- Helpers ----------------

    def _update_enabled_state(self):
        has_root = bool(self.root_dir)
        has_session = self.state.is_loaded()

        self.btn_create.setEnabled(has_root)
        self.btn_import.setEnabled(has_root)

        # Base enable; refined below via _update_play_pause_buttons()
        self.btn_play.setEnabled(has_session)
        self.btn_pause.setEnabled(has_session)
        if hasattr(self, "btn_restart"):
            self.btn_restart.setEnabled(has_session)

        self.btn_start_step.setEnabled(has_session)
        self.combo_views.setEnabled(has_session)
        self.combo_time_source.setEnabled(has_session)
        self.combo_audio_source.setEnabled(has_session)

        self.btn_delete_record.setEnabled(has_session)
        self.btn_finish_session.setEnabled(has_session)

        # Refine based on actual playback state.
        self._update_play_pause_buttons()

    def closeEvent(self, event):
        try:
            if self.cfg and self.root_dir:
                self.cfg.skills = self.skills_panel.skills()
                save_root_config(self.cfg)
        except Exception:
            pass

        try:
            if self.state.is_loaded():
                self.state.annotations = self.table.records()
                persist_session_state(self.state)
        except Exception:
            pass

        super().closeEvent(event)

    def _warn_if_session_skills_mismatch(self) -> None:
        """
        When importing/loading an older session, the root config skills list may have changed.
        Warn if annotation step numbers are missing from the current skills list, or if names differ.

        Notes:
          - This does not block import; it is informational.
          - The Timeline will still show the session annotations, but for consistent editing
            users should add/re-add missing steps or rename them in the Skills panel.
        """
        if not self.state.is_loaded():
            return
        if not self.cfg:
            return
        label = str(self.state.label or "")
        if self._warned_skill_mismatch_label == label:
            return

        # Build config map: step_no -> name
        cfg_map = {int(s.number): str(s.name) for s in (self.cfg.skills or [])}

        # Collect steps referenced by annotations
        seen = set()
        missing: List[str] = []
        renamed: List[str] = []

        for rec in (self.state.annotations or []):
            try:
                step_no = int(rec.step_no)
            except Exception:
                continue
            if step_no in seen:
                continue
            seen.add(step_no)

            sess_name = str(getattr(rec, "step_name", "") or "").strip()
            cfg_name = str(cfg_map.get(step_no, "")).strip()

            if step_no not in cfg_map:
                # Missing from current config
                if sess_name:
                    missing.append(f"  • Step {step_no}: {sess_name}")
                else:
                    missing.append(f"  • Step {step_no}")
            else:
                # Present, but name may have changed
                if sess_name and cfg_name and sess_name != cfg_name:
                    renamed.append(f"  • Step {step_no}: session='{sess_name}' | config='{cfg_name}'")

        if not missing and not renamed:
            self._warned_skill_mismatch_label = label
            return

        parts: List[str] = []
        parts.append(
            "Some labels referenced in this session do not match your current labels list (config.json).\n"
            "The session annotations will still appear in the Timeline.\n\n"
            "To keep things consistent, you can edit labels in the right Labels panel: add missing labels, or rename labels to match."
        )

        if missing:
            parts.append("\nMissing labels (present in session annotations but not in current Labels list):")
            parts.extend(missing)

        if renamed:
            parts.append("\nName mismatches (same label number, different names):")
            parts.extend(renamed)

        QMessageBox.warning(self, "Skills mismatch", "\n".join(parts))
        self._warned_skill_mismatch_label = label