# video_annote/ui/dialogs/session_info.py
"""Session Info: each video's length, when it was recorded (which can be set here) and anything worth
knowing about it, in plain words. Technical details stay out of sight (log, `video-annote probe`)."""
from __future__ import annotations

import html
import re
from typing import Callable, List, Optional

from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox, QTextBrowser, QVBoxLayout, QWidget

from ...core.models import SessionState, VideoItem
from ...core.timecode import ms_to_time_str
from ...media.recorded import EnteredTime, end_of, local_text, precision_of, start_of, utc_offset_text
from ..common import hand_cursor_on_buttons
from ..widgets.start_time import ask_recording_start

# (video id, the time typed, or an empty one to use the file's time) -> raises ValueError if unusable
SetStart = Callable[[str, EnteredTime], None]


def source_name(v: VideoItem) -> str:
    """The file (or stream) a video was imported from, without its folder."""
    return re.split(r"[\\/]", str(v.source or "").split("?")[0].rstrip("/\\"))[-1] or v.filename


def recorded_text(v: VideoItem) -> str:
    """When ``v`` was recorded, in this computer's time zone, and where that comes from; or why unknown."""
    rec = v.recorded
    start = start_of(rec)
    if start is None:
        return ("Recording time not in the file" if rec else
                "Recording time not known yet (the original file was not found)")
    precision = precision_of(rec)
    text = local_text(start, precision)
    end = end_of(rec)
    if end is not None:
        same_day = end.astimezone().date() == start.astimezone().date()
        text += " → " + local_text(end, precision, date=not same_day)
    source = str(rec.get("from") or "")
    where = ("entered by hand" if source.startswith("entered") else
             "from the stream" if "stream" in source else "from the Tobii recording" if "Tobii" in source
             else "from the file")
    if "no time zone" in source:
        where += ", which gives no time zone"
    return f"Recorded {text} ({utc_offset_text(start)}), {where}"


def start_offsets(state: SessionState) -> str:
    """How far apart the videos started, by their recording times ("" with fewer than two known)."""
    known = [(v.video_id, start_of(v.recorded), precision_of(v.recorded)) for v in state.videos]
    known = [k for k in known if k[1] is not None]
    if len(known) < 2:
        return ""
    ref_id, ref, _ = known[0]
    decimals = 0 if any(p >= 1 for _, _, p in known) else 3
    parts = []
    for vid, start, _ in known[1:]:
        delta = (start - ref).total_seconds()  # type: ignore[operator]
        when = "at the same time as" if round(delta, decimals) == 0 else \
            f"{abs(delta):.{decimals}f} s {'after' if delta > 0 else 'before'}"
        parts.append(f"{vid} started {when} {ref_id}")
    return "By their recording times, " + "; ".join(parts) + "."


def _video_html(v: VideoItem, editable: bool) -> str:
    length = ms_to_time_str(v.duration_ms) if v.duration_ms else "unknown length"
    link = (f" &nbsp;<span style='white-space:nowrap'><a href='start:{html.escape(v.video_id)}'>Set start…</a></span>"
            if editable else "")
    lines = [f"<div style='margin-top:10px'><b>{html.escape(v.video_id)}</b> &nbsp; {html.escape(source_name(v))}</div>",
             f"<div style='color:#666'>{length} &nbsp;·&nbsp; {html.escape(recorded_text(v))}{link}</div>"]
    if v.notes:
        lines.append("<ul style='margin-top:2px; margin-bottom:0'>"
                     + "".join(f"<li>{html.escape(n)}</li>" for n in v.notes) + "</ul>")
    return "".join(lines)


def session_info_html(state: SessionState, notes: List[str], editable: bool = False) -> str:
    parts = [f"<div style='color:#888'>{html.escape(state.session_dir or '')}</div>"] if state.session_dir else []
    parts += [_video_html(v, editable) for v in state.videos]
    offsets = start_offsets(state)
    if offsets:
        parts.append(f"<p style='margin-top:12px'>{html.escape(offsets)}</p>")
    if notes:
        parts.append("<p style='margin-top:12px'><b>Good to know</b></p><ul style='margin-top:0'>"
                     + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>")
    return "".join(parts)


class SessionInfoDialog(QDialog):
    def __init__(self, state: SessionState, notes: List[str], title: str = "Session Info",
                 parent: Optional[QWidget] = None, set_start: Optional[SetStart] = None):
        super().__init__(parent)
        self._state, self._notes, self._set_start = state, notes, set_start
        self.setWindowTitle(f"{title} — {state.label}")
        self.resize(720, 460)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._on_link)
        layout.addWidget(self.browser)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy = buttons.addButton("Copy Text", QDialogButtonBox.ActionRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.browser.toPlainText()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        hand_cursor_on_buttons(buttons)
        self._render()

    def _render(self) -> None:
        self.browser.setHtml(session_info_html(self._state, self._notes, editable=self._set_start is not None))

    def _on_link(self, url: QUrl) -> None:
        target = url.toString()
        if not target.startswith("start:") or self._set_start is None:
            return
        v = self._state.get_video(target[len("start:"):])
        if v is None:
            return
        entered = str(v.recorded.get("from") or "").startswith("entered")
        current = str(v.recorded.get("start_unix") or "") if entered else ""
        value = ask_recording_start(self, f"{v.video_id} ({source_name(v)})", current, entered)
        if value is None:
            return
        try:
            self._set_start(v.video_id, value)
        except ValueError as e:
            QMessageBox.warning(self, "Recording start", str(e))
        self._render()
