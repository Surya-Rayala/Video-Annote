# video_annote/widgets/video_grid.py
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal, QSize
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import (
    QWidget,
    QGridLayout,
    QStackedWidget,
    QSizePolicy,
)

from ..domain import VideoItem


class VideoCell(QStackedWidget):
    """
    One grid cell: (0) video widget, (1) black placeholder.
    We switch to black when the time-source position exceeds this video's duration.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.black = QWidget()
        self.black.setStyleSheet("background-color: black;")
        self.black.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.addWidget(self.video)  # index 0
        self.addWidget(self.black)  # index 1
        self.setCurrentIndex(0)

    def show_black(self, yes: bool) -> None:
        self.setCurrentIndex(1 if yes else 0)

    def sizeHint(self) -> QSize:
        # Keep a stable size hint regardless of which stacked widget is visible.
        # QVideoWidget's hint can be (0,0) before media loads, so provide a small non-zero fallback.
        try:
            hint = self.video.sizeHint()
        except Exception:
            hint = QSize(0, 0)
        if hint.width() <= 0 or hint.height() <= 0:
            return QSize(160, 90)
        return hint

    def minimumSizeHint(self) -> QSize:
        # Avoid zero-size minimums that can cause cells to collapse in some grid arrangements.
        return QSize(80, 45)


class VideoGrid(QWidget):
    """
    Manages N QMediaPlayers + a 3-column video grid.

    Key behaviors:
      - Active view is a subset of videos (displayed in grid). Others are hidden but still loaded/synced.
      - Single time source (master clock) and audio source (volume=100, others 0).
      - Sync timer keeps slaves close to master when playing.
      - When time-source reaches end-of-media -> stop all players.
      - If master time exceeds a shorter video's duration -> display black in that cell.
    """

    # Emitted with the time-source position (ms)
    time_position_changed = pyqtSignal(int)
    # Emitted with the time-source duration (ms)
    time_duration_changed = pyqtSignal(int)
    # Emitted when time-source ends (all players have been stopped)
    time_source_ended = pyqtSignal()
    # Emitted when we detect/update fps for a video: (video_id, fps)
    fps_updated = pyqtSignal(str, float)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(4)

        # Track last configured grid shape so we can reset old stretch factors when the view count shrinks.
        self._last_grid_cols: int = 0
        self._last_grid_rows: int = 0

        # video_id -> (player, cell, item)
        self._players: Dict[str, QMediaPlayer] = {}
        self._cells: Dict[str, VideoCell] = {}
        self._items: Dict[str, VideoItem] = {}

        self._active_view_ids: List[str] = []
        self._time_source_id: Optional[str] = None
        self._audio_source_id: Optional[str] = None

        self._ignore_position_signals = False
        self._last_master_pos = 0

        # Buffer-aware play gate (helps during rapid scrubbing)
        self._last_media_status: Dict[str, int] = {}
        self._pending_play: bool = False
        self._pending_play_deadline: float = 0.0

        self._play_gate_timer = QTimer(self)
        self._play_gate_timer.setInterval(80)
        self._play_gate_timer.timeout.connect(self._play_gate_tick)

        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(200)
        self._sync_timer.timeout.connect(self._sync_tick)
        self._sync_timer.start()

    # ---------------- Public API ----------------

    def clear(self) -> None:
        self.pause_all()
        for vid, p in list(self._players.items()):
            try:
                p.stop()
                p.setMedia(QMediaContent())
            except Exception:
                pass
        self._players.clear()
        self._cells.clear()
        self._items.clear()
        self._active_view_ids = []
        self._time_source_id = None
        self._audio_source_id = None
        self._rebuild_grid()

    def load_videos(self, session_dir: str, videos: List[VideoItem]) -> None:
        """
        Load media for each VideoItem (expects filenames relative to session_dir).
        """
        self.clear()

        for v in (videos or []):
            vid = v.video_id
            self._items[vid] = v

            cell = VideoCell(self)
            self._cells[vid] = cell

            player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
            player.setVideoOutput(cell.video)

            # Connect signals
            player.positionChanged.connect(self._on_any_position_changed)
            player.durationChanged.connect(self._on_any_duration_changed)
            player.mediaStatusChanged.connect(lambda status, _vid=vid: self._on_media_status(_vid, status))
            try:
                player.bufferStatusChanged.connect(lambda _pct, _vid=vid: self._on_buffer_status(_vid))
            except Exception:
                pass

            # Load media if file exists
            path = os.path.join(session_dir, v.filename)
            if os.path.exists(path):
                player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
            else:
                # Still keep player; UI may show black
                player.setMedia(QMediaContent())

            # Default volumes to 0; audio source will be set later
            player.setVolume(0)

            self._players[vid] = player

        # Defaults: pick first as time/audio source, and first as active view.
        ids = list(self._players.keys())
        if ids:
            self.set_active_views([ids[0]])
            self.set_time_source(ids[0])
            self.set_audio_source(ids[0])

        self._rebuild_grid()

        # Show first frame immediately for visible videos
        self._prime_first_frame()

    def set_active_views(self, video_ids: List[str]) -> None:
        ids = [vid for vid in (video_ids or []) if vid in self._players]
        # if empty, keep at least one if possible
        if not ids and self._players:
            ids = [next(iter(self._players.keys()))]
        self._active_view_ids = ids
        self._rebuild_grid()
        # Update black placeholders based on current master time
        self._update_black_cells(self._current_master_position())

    def active_views(self) -> List[str]:
        return list(self._active_view_ids)

    def set_time_source(self, video_id: str) -> None:
        if video_id not in self._players:
            return
        self._time_source_id = video_id
        # Re-emit duration immediately (helps slider range update)
        dur = self._players[video_id].duration()
        self.time_duration_changed.emit(int(dur or 0))
        self._update_black_cells(self._current_master_position())

    def time_source(self) -> Optional[str]:
        return self._time_source_id

    def set_audio_source(self, video_id: str) -> None:
        if video_id not in self._players:
            return
        self._audio_source_id = video_id
        for vid, p in self._players.items():
            p.setVolume(100 if vid == video_id else 0)

    def audio_source(self) -> Optional[str]:
        return self._audio_source_id

    def fps_for(self, video_id: str) -> float:
        item = self._items.get(video_id)
        if item and item.fps and item.fps > 0:
            return float(item.fps)
        # fallback: try metadata
        p = self._players.get(video_id)
        if p is not None:
            try:
                fps = p.metaData("VideoFrameRate")
                if fps:
                    return float(fps)
            except Exception:
                pass
        return 30.0

    def duration_for(self, video_id: str) -> int:
        p = self._players.get(video_id)
        return int(p.duration() if p is not None else 0)

    # Playback control
    def play_all(self) -> None:
        """Play all players, but if media is still loading/buffering (common after rapid seeks),
        wait briefly until ready before starting playback."""
        if not self._players:
            return
        self._request_play_with_buffer_gate()

    def pause_all(self) -> None:
        for p in self._players.values():
            p.pause()

    def stop_all(self) -> None:
        for p in self._players.values():
            p.stop()

    def set_position_all(self, position_ms: int) -> None:
        """
        Seek all players to the same timeline position.
        """
        if not self._players:
            return
        was_playing = self.is_playing()
        if was_playing:
            # Avoid starting playback on partially-buffered frames while scrubbing
            self.pause_all()
        pos = int(max(0, position_ms))
        self._ignore_position_signals = True
        try:
            for p in self._players.values():
                p.setPosition(pos)
        finally:
            self._ignore_position_signals = False

        # refresh black cells relative to master
        self._update_black_cells(self._current_master_position())
        if was_playing:
            # Resume, but only once players look ready (or after a short timeout)
            self._request_play_with_buffer_gate()

    def is_playing(self) -> bool:
        master = self._time_source_player()
        if not master:
            return False
        return master.state() == QMediaPlayer.PlayingState

    # ---------------- Buffer-aware playback gate ----------------

    def _status_ready(self, status: int) -> bool:
        """Treat Loaded/Buffered/End as ready; allow NoMedia/Invalid to pass through (avoid deadlocks)."""
        try:
            return status in (
                QMediaPlayer.LoadedMedia,
                QMediaPlayer.BufferedMedia,
                QMediaPlayer.EndOfMedia,
                QMediaPlayer.NoMedia,
                QMediaPlayer.InvalidMedia,
            )
        except Exception:
            return True

    def _needs_buffer_gate(self) -> bool:
        """Return True if any relevant player is still loading/buffering/stalled."""
        if not self._players:
            return False

        # Prefer checking active views (the ones on screen). If none, fall back to all players.
        check_ids = self._active_view_ids or list(self._players.keys())

        for vid in check_ids:
            p = self._players.get(vid)
            if not p:
                continue
            try:
                st = int(p.mediaStatus())
            except Exception:
                st = int(self._last_media_status.get(vid, QMediaPlayer.UnknownMediaStatus))

            # If still loading/buffering/stalled, gate playback.
            if st in (QMediaPlayer.LoadingMedia, QMediaPlayer.BufferingMedia, QMediaPlayer.StalledMedia):
                return True

            # Unknown status right after a seek can also benefit from a short gate.
            if st == QMediaPlayer.UnknownMediaStatus:
                # If media exists, treat unknown as not-ready.
                try:
                    if p.mediaStatus() != QMediaPlayer.NoMedia:
                        return True
                except Exception:
                    return True

        return False

    def _request_play_with_buffer_gate(self) -> None:
        """If buffering is needed, wait briefly; otherwise play immediately."""
        if not self._players:
            return

        if not self._needs_buffer_gate():
            self._pending_play = False
            self._play_gate_timer.stop()
            for p in self._players.values():
                p.play()
            return

        # Defer playback briefly while buffering catches up.
        self._pending_play = True
        self._pending_play_deadline = time.monotonic() + 1.2  # seconds
        if not self._play_gate_timer.isActive():
            self._play_gate_timer.start()

    def _play_gate_tick(self) -> None:
        if not self._pending_play:
            self._play_gate_timer.stop()
            return

        # If buffering is no longer needed, or we hit the timeout, start playback.
        if (not self._needs_buffer_gate()) or (time.monotonic() >= float(self._pending_play_deadline)):
            self._pending_play = False
            self._play_gate_timer.stop()
            for p in self._players.values():
                p.play()

    def _on_buffer_status(self, vid: str) -> None:
        """Any buffer progress can be used as a cue to re-check readiness."""
        if self._pending_play:
            # Trigger an early gate tick
            self._play_gate_tick()

    # ---------------- Internal UI/grid ----------------

    def _grid_columns_for(self, n_items: int) -> int:
        """Choose a stable column count for the current number of visible videos.

        - 1 video  -> 1 column (use all width)
        - 2 videos -> 2 columns
        - 3+       -> 3 columns
        """
        if n_items <= 1:
            return 1
        if n_items == 2:
            return 2
        return 3

    def _rebuild_grid(self) -> None:
        # Clear layout items (do NOT de-parent the widgets; QVideoWidget can be sensitive to re-parenting).
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()

        n = len(self._active_view_ids)

        # Always reset old stretch factors so shrinking from (e.g.) 6 -> 1 doesn't keep a 3x2 grid geometry.
        # QGridLayout remembers stretch values for columns/rows even when they currently have no widgets.
        for col in range(max(self._last_grid_cols, 3)):
            self._grid.setColumnStretch(col, 0)
        for row in range(max(self._last_grid_rows, 3)):
            self._grid.setRowStretch(row, 0)

        if n <= 0:
            self._last_grid_cols = 0
            self._last_grid_rows = 0
            return

        cols = self._grid_columns_for(n)

        r = 0
        c = 0
        for vid in self._active_view_ids:
            cell = self._cells.get(vid)
            if cell is None:
                continue

            cell.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            cell.show()

            self._grid.addWidget(cell, r, c)
            c += 1
            if c >= cols:
                c = 0
                r += 1

        # Number of rows actually used
        rows = r + (1 if c > 0 else 0)
        rows = max(rows, 1)

        # Make the grid fill available space predictably.
        for col in range(cols):
            self._grid.setColumnStretch(col, 1)
        for row in range(rows):
            self._grid.setRowStretch(row, 1)

        self._last_grid_cols = cols
        self._last_grid_rows = rows

    def _prime_first_frame(self) -> None:
        """
        Show first frame for active views without starting playback.
        """
        for vid in self._active_view_ids:
            p = self._players.get(vid)
            if not p:
                continue
            try:
                p.setPosition(0)
                p.play()
                p.pause()
            except Exception:
                pass

    # ---------------- Time-source signal handlers ----------------

    def _time_source_player(self) -> Optional[QMediaPlayer]:
        if self._time_source_id and self._time_source_id in self._players:
            return self._players[self._time_source_id]
        return None

    def _current_master_position(self) -> int:
        master = self._time_source_player()
        if not master:
            return 0
        try:
            return int(master.position())
        except Exception:
            return 0

    def _on_any_position_changed(self, _pos: int) -> None:
        if self._ignore_position_signals:
            return
        master = self._time_source_player()
        if not master:
            return

        # Only broadcast when the sender is the master.
        # Qt doesn't give sender reliably in lambdas; use current master position each time.
        pos = int(master.position())
        if pos != self._last_master_pos:
            self._last_master_pos = pos
            self.time_position_changed.emit(pos)
            self._update_black_cells(pos)

    def _on_any_duration_changed(self, _dur: int) -> None:
        master = self._time_source_player()
        if not master:
            return
        dur = int(master.duration() or 0)
        self.time_duration_changed.emit(dur)

    def _on_media_status(self, vid: str, status) -> None:
        # Update fps from metadata when available
        p = self._players.get(vid)
        if not p:
            return
        try:
            self._last_media_status[vid] = int(status)
        except Exception:
            pass
        try:
            fps = p.metaData("VideoFrameRate")
            if fps:
                fps_f = float(fps)
                item = self._items.get(vid)
                if item and fps_f > 0 and abs(item.fps - fps_f) > 0.001:
                    item.fps = fps_f
                    self.fps_updated.emit(vid, fps_f)
        except Exception:
            pass

        # Stop all when time-source ends
        if self._time_source_id == vid and status == QMediaPlayer.EndOfMedia:
            self.stop_all()
            self.time_source_ended.emit()
        if self._pending_play:
            self._play_gate_tick()

    # ---------------- Sync logic ----------------

    def _sync_tick(self) -> None:
        """
        Keep all players aligned to the time source while playing.
        """
        master = self._time_source_player()
        if not master:
            return

        if master.state() != QMediaPlayer.PlayingState:
            return

        target = int(master.position() or 0)

        # If master ended but status signal didn't fire yet (backend-specific), stop all.
        mdur = int(master.duration() or 0)
        if mdur > 0 and target >= mdur:
            self.stop_all()
            self.time_source_ended.emit()
            return

        # Sync slaves if drift > 200ms
        for vid, p in self._players.items():
            if p is master:
                continue
            try:
                diff = abs(int(p.position() or 0) - target)
            except Exception:
                diff = 0
            if diff > 200:
                p.setPosition(target)
                # ensure slave plays if master is playing (keeps frames updating)
                if p.state() != QMediaPlayer.PlayingState:
                    p.play()

        self._update_black_cells(target)

    # ---------------- Black placeholder rule ----------------

    def _update_black_cells(self, master_pos_ms: int) -> None:
        """
        For each active view:
          - if its own duration is >0 and master_pos > duration -> show black
          - else show video
        """
        for vid in self._active_view_ids:
            cell = self._cells.get(vid)
            p = self._players.get(vid)
            if not cell or not p:
                continue
            dur = int(p.duration() or 0)
            if dur > 0 and master_pos_ms > dur:
                cell.show_black(True)
            else:
                cell.show_black(False)