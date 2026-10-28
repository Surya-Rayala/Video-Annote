# video_annote/ui/video_grid.py
"""Synchronized playback of the session's videos.

The time-source player is the only clock: everything the UI shows or records is read from it.
Behaviour of Qt 5.15's QMediaPlayer (verified on the AVFoundation backend) shapes this class:

* positionChanged fires every notifyInterval (default 1000 ms) and not at all on pause(), so the
  interval is lowered and positions are always read from the player when they matter.
* pause() or stop() on a player that reached EndOfMedia rewinds it to 0, so ended players are never
  paused or stopped; end of media becomes "paused on the last frame" via setPosition().
* Before a player has really played, its seeks are truncated to whole seconds (the backend reuses the
  timescale of currentTime, which is 1 until playback starts). Every player therefore "warms up" once
  after loading: it plays muted until its clock moves, then everything is paused and set back to 0.
* Seeks convert the position through float32 and can land microseconds before the target, so a
  seek to the exact start of a frame often shows the previous one. Paused players are therefore always
  held in the middle of a frame (see _park_ms).
* A player that reaches its end is stopped and its picture detached; only play() and pause() attach
  it again (setPosition() does not), and pause() at EndOfMedia rewinds to 0. Players are therefore
  re-attached after every seek while paused (see _hold).
* Players drift while playing; they are re-aligned to one reference on a timer, and exactly on every
  pause and seek. The audible player is the playback reference so the sound itself is never jumped
  (a seek on a playing AVPlayer drops 85-170 ms of audio). Corrections lead the target by the typical
  seek latency and are rate-limited per player, so a corrected player does not land behind again.
"""
from __future__ import annotations

import math
import os
import time
from fractions import Fraction
from typing import Dict, List, Optional

from PyQt5.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import QGridLayout, QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from ..core.models import VideoItem
from ..core.timecode import displayed_frame, frame_at_ms, frame_centre_ms, frame_start_ms
from .common import ElidedLabel

NOTIFY_INTERVAL_MS = 40
SYNC_INTERVAL_MS = 250
DRIFT_TOLERANCE_MS = 120
SEEK_LEAD_MS = 150  # a corrective seek completes ~130-200 ms after it is issued
CORRECTION_COOLDOWN_S = 1.5
PLAY_GATE_TIMEOUT_S = 1.2
WARMUP_TIMEOUT_S = 6.0


class VideoCell(QWidget):
    """One grid cell: which recording it is, above the video (or black once the timeline is past
    this video's end)."""

    def __init__(self, caption: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.caption = ElidedLabel(caption)
        self.caption.setStyleSheet("color: #888;")
        self.stack = QStackedWidget()
        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.black = QWidget()
        self.black.setStyleSheet("background-color: black;")
        self.stack.addWidget(self.video)
        self.stack.addWidget(self.black)
        layout.addWidget(self.caption)
        layout.addWidget(self.stack, stretch=1)

    def show_black(self, yes: bool) -> None:
        self.stack.setCurrentIndex(1 if yes else 0)

    def is_black(self) -> bool:
        return self.stack.currentIndex() == 1

    def sizeHint(self) -> QSize:
        hint = self.video.sizeHint()
        hint = hint if hint.width() > 0 and hint.height() > 0 else QSize(160, 90)
        return QSize(hint.width(), hint.height() + self.caption.sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(80, 45 + self.caption.sizeHint().height())


class VideoGrid(QWidget):
    position_changed = pyqtSignal(int)  # time-source position (ms)
    duration_changed = pyqtSignal(int)  # time-source duration (ms)
    playing_changed = pyqtSignal(bool)
    reached_end = pyqtSignal()
    ready_changed = pyqtSignal(bool)  # False while players load and warm up

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(4)
        self._placeholder = QLabel(self)  # what to do next, while no session is open
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color: #888; font-size: 15px; padding: 24px;")
        self._grid.addWidget(self._placeholder, 0, 0)

        self._players: Dict[str, QMediaPlayer] = {}
        self._cells: Dict[str, VideoCell] = {}
        self._items: Dict[str, VideoItem] = {}
        self._active: List[str] = []
        self._time_source: Optional[str] = None
        self._audio_source: Optional[str] = None

        self._playing = False  # user intent (the backend state lags behind)
        self._at_end = False
        self._ready = False
        self._warming: Dict[str, bool] = {}  # video id -> warm-up playback started
        self._warm_deadline = 0.0
        self._pending_seek: Optional[int] = None
        self._last_fix: Dict[str, float] = {}
        self._play_deadline: Optional[float] = None

        self._gate_timer = QTimer(self)
        self._gate_timer.setInterval(80)
        self._gate_timer.timeout.connect(self._gate_tick)
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self._sync_tick)
        self._warm_timer = QTimer(self)
        self._warm_timer.setInterval(30)
        self._warm_timer.timeout.connect(self._warm_tick)

    # ---------------- loading ----------------

    def clear(self) -> None:
        self._set_playing(False)
        self._gate_timer.stop()
        self._sync_timer.stop()
        self._warm_timer.stop()
        self._play_deadline = None
        self._warming = {}
        self._pending_seek = None
        self._last_fix = {}
        self._set_ready(False)
        for player in self._players.values():
            try:
                player.blockSignals(True)
                player.stop()
                player.setMedia(QMediaContent())
            finally:
                player.deleteLater()
        for cell in self._cells.values():
            self._grid.removeWidget(cell)
            cell.setParent(None)
            cell.deleteLater()
        self._players.clear()
        self._cells.clear()
        self._items.clear()
        self._active = []
        self._time_source = self._audio_source = None
        self._at_end = False
        self._rebuild_grid()

    def set_placeholder(self, text: str) -> None:
        self._placeholder.setText(text)

    def load(self, session_dir: str, videos: List[VideoItem]) -> None:
        self.clear()
        for item in videos:
            vid = item.video_id
            self._items[vid] = item
            cell = VideoCell(item.display_name, self)
            cell.hide()
            self._cells[vid] = cell
            player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
            player.setVideoOutput(cell.video)
            player.setNotifyInterval(NOTIFY_INTERVAL_MS)
            player.setVolume(0)
            player.positionChanged.connect(lambda pos, v=vid: self._on_position(v, pos))
            player.durationChanged.connect(lambda dur, v=vid: self._on_duration(v, dur))
            player.mediaStatusChanged.connect(lambda st, v=vid: self._on_status(v, st))
            path = os.path.abspath(os.path.join(session_dir, item.filename))  # a relative URL never opens
            player.setMedia(QMediaContent(QUrl.fromLocalFile(path)) if os.path.exists(path) else QMediaContent())
            self._players[vid] = player
        ids = list(self._players)
        self._warming = {vid: False for vid in ids}
        if ids:
            self._time_source = ids[0]
            self.set_active_views([ids[0]])
            self.set_audio_source(ids[0])
        self._warm_deadline = time.monotonic() + WARMUP_TIMEOUT_S
        self._warm_timer.start()
        self._sync_timer.start()

    # ---------------- selection ----------------

    def video_ids(self) -> List[str]:
        return list(self._players)

    def set_active_views(self, video_ids: List[str]) -> None:
        ids = [v for v in video_ids if v in self._players] or self.video_ids()[:1]
        newly_shown = [v for v in ids if v not in self._active]
        self._active = ids
        self._rebuild_grid()
        if self._ready:
            pos = self.position()
            for vid in newly_shown:  # bring a just-shown player to the current frame (and running)
                player = self._players[vid]
                if self._playing:
                    player.setPosition(min(pos + SEEK_LEAD_MS, self._last_frame_ms(vid)))
                    if self._within(vid, pos):
                        player.play()
                else:
                    self._hold(player, self._park_ms(vid, self._frame_at(pos)))
            if self._playing:
                for vid, player in self._players.items():
                    if not self._decodes(vid) and player.state() == QMediaPlayer.PlayingState:
                        player.pause()
        self._update_black_cells(self.position())

    def active_views(self) -> List[str]:
        return list(self._active)

    def time_source(self) -> Optional[str]:
        return self._time_source

    def set_time_source(self, video_id: str) -> None:
        """Switch the master clock, keeping the current timeline position (clamped to the new
        master's last frame). Callers pause before and resume after if playback should continue."""
        if video_id not in self._players or video_id == self._time_source:
            return
        pos = self.position() if self._ready else (self._pending_seek or 0)  # warm-up moves the clock
        self._time_source = video_id
        self.duration_changed.emit(self.duration())
        self.seek(min(pos, self._last_frame_ms(video_id)))

    def audio_source(self) -> Optional[str]:
        return self._audio_source

    def set_audio_source(self, video_id: str) -> None:
        if video_id not in self._players:
            return
        self._audio_source = video_id
        if self._warming:  # players are playing muted to warm up; volumes are applied when ready
            return
        for vid, player in self._players.items():
            player.setMuted(vid != video_id)
            player.setVolume(100 if vid == video_id else 0)

    # ---------------- facts ----------------

    def fps_for(self, video_id: str) -> Fraction:
        item = self._items.get(video_id)
        return item.exact_fps if item else Fraction(30)

    def frame_ms(self, video_id: Optional[str] = None) -> float:
        return 1000.0 / float(self.fps_for(video_id or self._time_source or ""))

    def duration(self, video_id: Optional[str] = None) -> int:
        """Length of the *video track* in ms. The player's duration is the longest track, and audio
        often outlasts the video (by 0.3 s in the room recordings): no frame exists there."""
        vid = video_id or self._time_source or ""
        item = self._items.get(vid)
        if item is not None and item.frame_count:
            return frame_start_ms(item.frame_count, item.exact_fps)
        player = self._players.get(vid)
        if player is not None and player.duration() > 0:
            return int(player.duration())
        return int(item.duration_ms) if item else 0

    def last_frame(self, video_id: Optional[str] = None) -> int:
        """Index of the last video frame."""
        vid = video_id or self._time_source or ""
        item = self._items.get(vid)
        if item is not None and item.frame_count:
            return item.frame_count - 1
        return max(0, frame_at_ms(max(0, self.duration(vid) - 1), self.fps_for(vid)))

    def _last_frame_ms(self, video_id: str) -> int:
        return frame_centre_ms(self.last_frame(video_id), self.fps_for(video_id))

    def frame_at(self, position_ms: int) -> int:
        """Time-source frame shown when the timeline is at ``position_ms``."""
        return self._frame_at(position_ms)

    def mapped_frame(self, video_id: str, master_frame: int) -> int:
        """Frame of ``video_id`` shown (and exported) while the time source shows ``master_frame``."""
        return self._mapped_frame(video_id, master_frame)

    def failed_videos(self) -> List[str]:
        """Videos the player could not open."""
        return [v for v, p in self._players.items()
                if p.mediaStatus() in (QMediaPlayer.InvalidMedia, QMediaPlayer.NoMedia)]

    def _frame_at(self, position_ms: int) -> int:
        """Time-source frame on screen at a time-source position (never past its last frame)."""
        vid = self._time_source or ""
        return min(frame_at_ms(max(0, int(position_ms)), self.fps_for(vid)), self.last_frame(vid))

    def position(self) -> int:
        """Current time-source position in ms, read from the player (never from a widget)."""
        player = self._players.get(self._time_source or "")
        if player is None:
            return 0
        return max(0, min(int(player.position()), self.duration()))

    def is_playing(self) -> bool:
        return self._playing

    def is_ready(self) -> bool:
        return self._ready

    def at_end(self) -> bool:
        return self._at_end

    # ---------------- transport ----------------

    def play(self) -> None:
        if not self._players or self._playing or not self._ready:
            return
        if self._at_end or self._frame_at(self.position()) >= self.last_frame():
            self._at_end = True
            return
        self._set_playing(True)
        if self._needs_buffering():
            self._play_deadline = time.monotonic() + PLAY_GATE_TIMEOUT_S
            self._gate_timer.start()
        else:
            self._start_players()

    def pause(self) -> None:
        """Pause everything and park all players on the master's frame."""
        self._play_deadline = None
        self._gate_timer.stop()
        if not self._ready:  # never interrupt the muted warm-up playback (see _warm_tick)
            self._set_playing(False)
            return
        for player in self._players.values():
            if player.state() == QMediaPlayer.PlayingState and player.mediaStatus() != QMediaPlayer.EndOfMedia:
                player.pause()
        self._set_playing(False)
        if self._at_end:
            self.position_changed.emit(self.position())
        else:
            self._park(self._frame_at(self.position()))

    def seek(self, position_ms: int) -> None:
        """Show the time-source frame at ``position_ms`` on the timeline, every player lined up on it
        (players stay paused or playing)."""
        if not self._players:
            return
        frame = self._frame_at(position_ms)
        if not self._ready:  # applied once warm-up is over (earlier seeks would be truncated)
            self._pending_seek = frame_centre_ms(frame, self.fps_for(self._time_source or ""))
            self.position_changed.emit(self._pending_seek)
            return
        self._at_end = False
        self._park(frame)

    def step_frames(self, frames: int) -> None:
        """Show the frame ``frames`` away from the one on screen."""
        vid = self._time_source or ""
        target = max(0, min(self._frame_at(self.position()) + frames, self.last_frame(vid)))
        self.seek(frame_centre_ms(target, self.fps_for(vid)))

    # ---------------- internals ----------------

    def _set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.playing_changed.emit(playing)

    def _set_ready(self, ready: bool) -> None:
        if ready != self._ready:
            self._ready = ready
            self.ready_changed.emit(ready)

    def _warm_tick(self) -> None:
        """Play each loaded player (muted) until its clock moves, then park everything at the start."""
        for vid, started in list(self._warming.items()):
            player = self._players[vid]
            status = player.mediaStatus()
            if status in (QMediaPlayer.InvalidMedia, QMediaPlayer.NoMedia):
                del self._warming[vid]
            elif not started and status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
                player.setMuted(True)
                player.play()
                self._warming[vid] = True
            elif started and (player.position() >= 40 or status == QMediaPlayer.EndOfMedia):
                if status != QMediaPlayer.EndOfMedia:
                    player.pause()
                del self._warming[vid]
        if self._warming and time.monotonic() < self._warm_deadline:
            return
        self._warm_timer.stop()
        for player in self._players.values():
            if player.state() == QMediaPlayer.PlayingState:
                player.pause()
        self._warming = {}
        self._set_ready(True)
        self.set_audio_source(self._audio_source or "")
        target, self._pending_seek = self._pending_seek or 0, None
        self.seek(target)

    def _decodes(self, vid: str) -> bool:
        """Hidden videos that are neither the clock nor the audible one stay paused while playing
        (they are re-aligned on every pause/seek and started when shown), saving decoder work."""
        return vid in self._active or vid == self._time_source or vid == self._audio_source

    def _within(self, vid: str, pos: int) -> bool:
        dur = self.duration(vid)
        return dur <= 0 or pos < dur - self.frame_ms(vid) / 2

    def _park(self, frame: int) -> None:
        """Hold every player on the master's ``frame`` and report the master's position."""
        for vid, player in self._players.items():
            self._hold(player, self._park_ms(vid, frame))
        pos = frame_centre_ms(frame, self.fps_for(self._time_source or ""))
        self._update_black_cells(pos, frame)
        self.position_changed.emit(pos)

    def _hold(self, player: QMediaPlayer, pos: int) -> None:
        """Seek ``player``; when the grid is paused, also make sure it shows the frame (see the module
        notes: a player that reached its end has no picture until play() or pause())."""
        player.setPosition(pos)
        if player.mediaStatus() == QMediaPlayer.EndOfMedia:  # setPosition() ignores an unchanged position
            player.setPosition(max(0, pos - 1))
            player.setPosition(pos)
        if not self._playing:
            player.pause()

    def _mapped_frame(self, vid: str, master_frame: int) -> int:
        """Frame of ``vid`` that labels and exports use while the master shows ``master_frame``:
        displayed_frame() of the time as label.tsv stores it, with the video's own rate and start
        (may be past its last frame)."""
        master = self._time_source or ""
        if vid == master:
            return master_frame
        item = self._items.get(vid)
        start_s = float((item.media.get("video_start_s") if item else None) or 0.0)
        t = round(float(Fraction(master_frame) / self.fps_for(master)), 3)
        return displayed_frame(max(0.0, t - start_s), self.fps_for(vid))

    def _park_ms(self, vid: str, master_frame: int) -> int:
        """Where ``vid`` is held while the master shows ``master_frame``: the middle of that frame for
        the master, and of its mapped frame (_mapped_frame) for every other video."""
        master = self._time_source or ""
        if vid == master:
            return frame_centre_ms(master_frame, self.fps_for(master))
        item = self._items.get(vid)
        fps = self.fps_for(vid)
        start_s = float((item.media.get("video_start_s") if item else None) or 0.0)
        frame = min(self._mapped_frame(vid, master_frame), self.last_frame(vid))
        return int(math.floor(start_s * 1000.0 + (frame + 0.5) * 1000.0 / float(fps)))

    def _start_players(self) -> None:
        pos = self.position()
        for vid, player in self._players.items():
            if self._within(vid, pos) and self._decodes(vid):
                player.play()

    def _needs_buffering(self) -> bool:
        busy = (QMediaPlayer.LoadingMedia, QMediaPlayer.BufferingMedia, QMediaPlayer.StalledMedia)
        return any(self._players[v].mediaStatus() in busy for v in self._active if v in self._players)

    def _gate_tick(self) -> None:
        if self._play_deadline is None:
            self._gate_timer.stop()
            return
        if not self._needs_buffering() or time.monotonic() >= self._play_deadline:
            self._gate_timer.stop()
            self._play_deadline = None
            if self._playing:
                self._start_players()

    def _on_position(self, vid: str, pos: int) -> None:
        if vid != self._time_source or not self._ready:
            return
        self._update_black_cells(pos)
        self.position_changed.emit(max(0, min(int(pos), self.duration())))

    def _on_duration(self, vid: str, _dur: int) -> None:
        if vid == self._time_source:
            self.duration_changed.emit(self.duration())

    def _on_status(self, vid: str, status: int) -> None:
        if not self._ready or status != QMediaPlayer.EndOfMedia:
            return
        if vid == self._time_source:
            self._handle_end()
        elif not self._playing:  # another video ended just after the grid stopped: hold it again
            self._hold(self._players[vid], self._park_ms(vid, self._frame_at(self.position())))

    def _handle_end(self) -> None:
        """Master reached the end: park every player on its last frame and report the end once."""
        self._play_deadline = None
        self._gate_timer.stop()
        self._at_end = True
        self._set_playing(False)
        last = self.last_frame()
        for player in self._players.values():  # pause first: a seek issued while playing and followed by
            if player.state() == QMediaPlayer.PlayingState and player.mediaStatus() != QMediaPlayer.EndOfMedia:
                player.pause()  # pause() may never present the sought frame
        for vid, player in self._players.items():
            self._hold(player, self._park_ms(vid, last))
        self._update_black_cells(self._last_frame_ms(self._time_source or ""), last)
        self.position_changed.emit(self._last_frame_ms(self._time_source or ""))
        self.reached_end.emit()

    def _sync_tick(self) -> None:
        if not self._playing or self._play_deadline is not None:
            return
        master = self._players.get(self._time_source or "")
        if master is None:
            return
        ref_id = self._time_source
        audio = self._players.get(self._audio_source or "")
        if audio is not None and audio.state() == QMediaPlayer.PlayingState:
            ref_id = self._audio_source  # never jump the audible track; move the others to it
        target = int(self._players[ref_id or ""].position())
        now = time.monotonic()
        for vid, player in self._players.items():
            if vid == ref_id or not self._within(vid, target) or not self._decodes(vid):
                continue  # parked: past its end (cell shows black) or hidden
            if player.mediaStatus() == QMediaPlayer.EndOfMedia:
                continue  # ended (play() would restart it from 0); a seek clears this
            drift = int(player.position()) - target
            if abs(drift) > DRIFT_TOLERANCE_MS and now - self._last_fix.get(vid, 0.0) > CORRECTION_COOLDOWN_S:
                player.setPosition(min(target + SEEK_LEAD_MS, self._last_frame_ms(vid)))
                self._last_fix[vid] = now
            if player.state() != QMediaPlayer.PlayingState:
                player.play()
        self._update_black_cells(int(master.position()))

    def _update_black_cells(self, master_pos: int, master_frame: Optional[int] = None) -> None:
        """Black out videos that have ended. While paused, a video is black exactly when labels and
        exports have no frame of it for the master's frame."""
        if master_frame is None and not self._playing and self._ready:
            master_frame = self._frame_at(master_pos)
        for vid in self._active:
            cell = self._cells.get(vid)
            if cell is None:
                continue
            if master_frame is not None and vid != self._time_source:
                cell.show_black(self._mapped_frame(vid, master_frame) > self.last_frame(vid))
            else:
                dur = self.duration(vid)
                cell.show_black(dur > 0 and master_pos >= dur)

    def _rebuild_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
        # QGridLayout remembers stretch for rows/columns that are now empty; reset all of them so a
        # shrinking selection does not keep the old geometry.
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        for r in range(self._grid.rowCount()):
            self._grid.setRowStretch(r, 0)
        if not self._active:
            self._grid.addWidget(self._placeholder, 0, 0)
            self._placeholder.show()
            self._grid.setRowStretch(0, 1)
            self._grid.setColumnStretch(0, 1)
            return
        n = len(self._active)
        cols = 1 if n <= 1 else 2 if n == 2 else 3
        for i, vid in enumerate(self._active):
            cell = self._cells[vid]
            self._grid.addWidget(cell, i // cols, i % cols)
            cell.show()
        for c in range(cols):
            self._grid.setColumnStretch(c, 1)
        for r in range((n + cols - 1) // cols or 1):
            self._grid.setRowStretch(r, 1)
