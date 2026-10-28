# video_annote/sessions.py
"""Session-level operations shared by the GUI and the command line (no Qt here)."""
from __future__ import annotations

import dataclasses
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .core.models import RootConfig, SessionState, VideoItem, decode_camid
from .core.storage import (
    load_session_state, move_to_trash, persist_session_state, session_dir, timestamp,
    validate_session_label,
)
from .core.timecode import seconds_to_frames
from .media.ffmpeg import CancelCheck, CancelledError, FFmpegError
from .media.ingest import IngestOptions, _hls_playlist, ingest, keeps_timestamps
from .media.probe import MediaInfo, probe
from .media.recorded import EnteredTime, entered_stamp, find_recording_time, recorded_block, upgrade_block

ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".mts", ".m2ts", ".ts"}
ALLOWED_URL_EXTS = ALLOWED_VIDEO_EXTS | {".m3u8"}


def is_url(text: str) -> bool:
    from urllib.parse import urlparse
    p = urlparse(text.strip())
    return p.scheme in ("http", "https") and bool(p.netloc)


def _ext(path_or_url: str) -> str:
    return os.path.splitext(path_or_url.strip().split("?")[0].split("#")[0])[1].lower()


def validate_local_video_path(path: str) -> Tuple[bool, str]:
    if not path:
        return False, "No file selected."
    if not os.path.isfile(path):
        return False, f"File does not exist: {path}"
    if _ext(path) not in ALLOWED_VIDEO_EXTS:
        return False, f"Unsupported file type '{_ext(path)}'. Allowed: {', '.join(sorted(ALLOWED_VIDEO_EXTS))}"
    return True, "OK"


def validate_video_url(url: str) -> Tuple[bool, str]:
    if not url or not is_url(url):
        return False, "Invalid URL (expected http:// or https://)."
    ext = _ext(url)
    if ext and ext not in ALLOWED_URL_EXTS:
        return False, f"Unsupported URL type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_URL_EXTS))}"
    return True, "OK"


@dataclass(frozen=True)
class SourceSpec:
    video_id: str
    source: str
    source_type: str  # "local" | "url"
    start: EnteredTime = EnteredTime("")  # when the recording started, as typed by hand (optional)


@dataclass
class CreateResult:
    state: SessionState
    warnings: List[str] = field(default_factory=list)  # technical, per video (see ingest)
    replaced_to: Optional[str] = None
    notes: List[str] = field(default_factory=list)  # what people need to know, in plain words, per video


def create_session(
    root_dir: str,
    label: str,
    sources: List[SourceSpec],
    *,
    replace: bool = False,
    options: IngestOptions = IngestOptions(),
    on_progress: Optional[Callable[[float, str], None]] = None,
    should_cancel: Optional[CancelCheck] = None,
) -> CreateResult:
    """Import ``sources`` into a new session folder. Everything is built in a hidden staging folder
    and moved into place only when every video imported and validated; on failure or cancel nothing
    is left behind. With ``replace``, an existing session folder is moved to the trash, never deleted."""
    error = validate_session_label(label)
    if error:
        raise ValueError(error)
    if not sources:
        raise ValueError("Add at least one video.")
    # (ValueError if a typed start is unusable)
    starts = {spec.video_id: entered_stamp(spec.start) for spec in sources if spec.start.text.strip()}
    final = session_dir(root_dir, label)
    if os.path.exists(final) and not replace:
        raise FileExistsError(f"Session '{label}' already exists at {final}")

    staging = os.path.join(root_dir, f".{label}.importing-{timestamp()}")
    os.makedirs(staging)
    result = CreateResult(state=SessionState(root_dir=root_dir, label=label, session_dir=staging))
    try:
        n = len(sources)
        for i, spec in enumerate(sources):
            name = f"{spec.video_id}.mp4"

            def report(fraction: float, _i: int = i, _spec: SourceSpec = spec) -> None:
                if on_progress is not None:
                    on_progress((_i + fraction) / n, f"Importing {_spec.video_id} ({_i + 1}/{n}): "
                                                      f"{os.path.basename(_spec.source)}")

            _check_cancel(should_cancel)  # probing and audio analysis do not poll it
            report(0.0)
            r = ingest(spec.source, os.path.join(staging, name), options, report, should_cancel,
                       start=starts.get(spec.video_id))
            result.warnings += [f"{spec.video_id}: {w}" for w in r.warnings]
            result.notes += [f"{spec.video_id}: {n}" for n in r.notes]
            result.state.videos.append(VideoItem(
                video_id=spec.video_id, filename=name, source_type=spec.source_type, source=spec.source,
                duration_ms=r.duration_ms, fps=float(r.frame_rate),
                frame_rate=f"{r.frame_rate.numerator}/{r.frame_rate.denominator}",
                frame_count=r.frame_count, media=r.media, recorded=r.recorded, notes=r.notes,
                imported=f"{time.strftime('%Y-%m-%d %H:%M')} with video-annote {__version__}",
            ))
        result.state.ensure_default_sources()
        persist_session_state(result.state)
        _check_cancel(should_cancel)  # last chance: after this the session appears (and may replace one)
        if os.path.exists(final):
            if not replace:  # someone created it while we were importing
                raise FileExistsError(f"Session '{label}' was created elsewhere during the import: {final}")
            result.replaced_to = move_to_trash(final, root_dir)
        try:
            os.rename(staging, final)
        except OSError:
            if result.replaced_to:  # put the previous session back where it was
                shutil.move(result.replaced_to, final)
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    result.state.session_dir = final
    return result


def _check_cancel(should_cancel: Optional[CancelCheck]) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError()


def _look_up_recorded(v: VideoItem, copy: MediaInfo) -> Optional[Dict[str, Any]]:
    """The "recorded" block of a video imported before it was stored: from the recording it was
    imported from when that is still in place and is the same recording (same length), else from
    the working copy's own metadata. None while neither can tell (the original may come back, e.g. on
    a drive that is not connected now)."""
    end = copy.video.start_time + (copy.video_duration or 0.0) if copy.video else 0.0
    if v.source_type == "local" and v.source and os.path.isfile(v.source) and end:
        try:
            info: Optional[MediaInfo] = probe(v.source)
        except FFmpegError:
            info = None
        if info is not None and info.video is not None:
            origin = 0.0 if keeps_timestamps(info) else info.start_time
            # the same recording: the same length (an HLS stream's as its playlist states it)
            playlist = _hls_playlist(v.source, info, 30.0) if "hls" in info.format_name else None
            if playlist is not None:
                same = abs(playlist.total_s - end) <= playlist.precision_s + 0.5
            else:
                same = bool(info.video.duration) and abs(info.video.start_time + (info.video.duration or 0.0)
                                                         - origin - end) <= 0.5
            if same:
                stamp = find_recording_time(v.source, info, playlist.program_date_time if playlist else None)
                return recorded_block(stamp, origin, info.start_time, end)
    stamp = find_recording_time(copy.path, copy)
    if stamp is not None or v.source_type == "url":  # (a stream is not downloaded again to look)
        return recorded_block(stamp, 0.0, copy.start_time, end)
    return None


def set_recording_start(state: SessionState, video_id: str, entered: EnteredTime) -> None:
    """Record that ``video_id`` started at a time typed by hand (ValueError if unusable); an empty
    one forgets it, so the time the recording itself states is looked up again."""
    v = state.get_video(video_id)
    if v is None:
        raise ValueError(f"no video {video_id}")
    if not entered.text.strip():
        v.recorded = {}
        refresh_video_facts(state)
        return
    end = v.media.get("video_start_s") or 0.0
    end = float(end) + (v.frame_count / float(v.exact_fps) if v.frame_count else v.duration_ms / 1000.0)
    v.recorded = recorded_block(entered_stamp(entered), 0.0, 0.0, end)


def refresh_video_facts(state: SessionState) -> List[str]:
    """Fill in exact frame rate, frame count, duration and video start for videos that lack them
    (sessions created by 0.1.x stored a float fps that fell back to 30.0 whenever probing failed),
    and when each recording was made (sessions created before 0.2 kept it nowhere).
    Working copies are measured as they are: their own rate is used, never a snapped one. A video
    that cannot be read is noted and the others are still measured; FFmpegNotFoundError is raised
    when ffprobe is missing."""
    notes: List[str] = []
    for v in state.videos:
        v.recorded = upgrade_block(v.recorded)  # (as 0.2 previews wrote it)
        facts = not (v.frame_rate and "video_start_s" in v.media)
        if not facts and v.recorded:
            continue
        path = os.path.join(state.session_dir or "", v.filename)
        if not os.path.isfile(path):
            continue
        try:
            info = probe(path)
        except FFmpegError as e:
            if facts:
                notes.append(f"{v.video_id}: {v.filename} could not be read ({e.summary}); "
                             + (f"the frame rate stored in session.json ({float(v.exact_fps):g} fps) is used"
                                if v.rate_known else "its frame rate is unknown"))
            continue
        if not v.recorded:
            found = _look_up_recorded(v, info)
            if found is not None:
                v.recorded = found
        if not facts or info.video is None:
            continue
        rate = info.video.exact_frame_rate()
        if rate and not v.frame_rate:
            if v.fps and abs(float(rate) - float(v.fps)) > 0.01:  # (inferred videos have no stored rate)
                notes.append(f"{v.video_id}: frame rate is {float(rate):.3f} fps (session.json said {v.fps:g})")
            v.frame_rate = f"{rate.numerator}/{rate.denominator}"
            v.fps = float(rate)
        if info.video.frame_count:
            v.frame_count = info.video.frame_count
        if info.video_duration:
            v.duration_ms = int(round(info.video_duration * 1000))
        v.media["video_start_s"] = round(info.video.start_time, 6)
        redo = ("for exact frames, open and finish this session once in this version (so its notes are "
                "saved in the current format), import the same videos again, in the same order, into a new "
                "session and copy this session's label.tsv into it (its frame numbers are recomputed when "
                "that session is opened)")
        if info.video.is_variable_rate:
            v.media["variable_frame_rate"] = True
            notes.append(f"{v.video_id}: the working copy has a variable frame rate (made by an older version), "
                         f"so frame numbers are approximate; {redo}")
        elif rate and abs(info.video.start_time - round(info.video.start_time * rate) / rate) > 0.0006:
            # (more than millisecond rounding away from a frame time)
            notes.append(f"{v.video_id}: its video starts at {info.video.start_time:.3f} s, between two frame times "
                         "(made by an older version); while it is the time source, the frame shown can be one "
                         f"away from the frame its clips start with; {redo}")
    return notes


def sync_label_frames(state: SessionState) -> List[int]:
    """Recompute the frame numbers of labels whose frames do not match their times at their time
    source's frame rate: a label.tsv copied from another session, edited by hand, or written by an
    older version that stored a wrong rate. The times are kept: they are what the timeline shows and
    what clips are cut from. Labels whose time source or its rate is unknown are left as they are.
    Returns the (1-based) rows that changed (see frame_mismatch_note)."""
    rows: List[int] = []
    for i, rec in enumerate(state.annotations):
        item = state.get_video(rec.time_source)
        if item is None or not item.rate_known or rec.start_time < 0 or rec.end_time < rec.start_time:
            continue
        sf, ef = seconds_to_frames(rec.start_time, item.exact_fps), seconds_to_frames(rec.end_time, item.exact_fps)
        if (rec.start_frame, rec.end_frame, rec.total_frames) != (sf, ef, ef - sf):
            state.annotations[i] = dataclasses.replace(rec, start_frame=sf, end_frame=ef, total_frames=ef - sf)
            rows.append(i + 1)
    return rows


def frame_mismatch_note(rows: List[int], consequence: str) -> str:
    listed = ", ".join(str(r) for r in rows[:12]) + (", …" if len(rows) > 12 else "")
    return (f"label.tsv: the frame numbers of {len(rows)} label(s) (rows {listed}) do not match their times at the "
            f"time source's frame rate (e.g. the file was copied from another session); {consequence}")


# -----------------------------
# Validation (dataset QA)
# -----------------------------

@dataclass
class ValidationReport:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_session(root_dir: str, label: str, cfg: Optional[RootConfig] = None,
                     probe_media: bool = True) -> ValidationReport:
    """Check that a session is complete and its annotations are consistent with its videos."""
    rep = ValidationReport()
    if not os.path.isdir(session_dir(root_dir, label)):
        rep.errors.append("session folder does not exist")
        return rep
    state, load = load_session_state(root_dir, label, backup=False)  # read-only: never write backups
    rep.warnings += load.warnings
    rep.errors += [f"label.tsv line {n}: unreadable ({why})" for n, why in load.skipped_rows]
    if not state.videos:
        rep.errors.append("session has no videos")
        return rep

    durations = {}
    # the rates labels are numbered with: session.json's exact rate, else (0.1.x float rates, or none)
    # the working copy's measured one, as the GUI uses them (see refresh_video_facts)
    rates = {v.video_id: v.exact_fps if v.rate_known else None for v in state.videos}
    for v in state.videos:
        path = os.path.join(state.session_dir or "", v.filename)
        if not os.path.isfile(path):
            rep.errors.append(f"{v.video_id}: missing file {v.filename}")
            continue
        durations[v.video_id] = v.duration_ms / 1000.0
        if not probe_media:
            if not v.rate_known:
                rep.warnings.append(f"{v.video_id}: session.json does not state its frame rate, so the frame numbers "
                                    "of its labels were not checked")
            continue
        info = probe(path)
        if info.video is None:
            rep.errors.append(f"{v.video_id}: no video stream")
            continue
        real = info.video_duration or 0.0
        durations[v.video_id] = real
        if v.duration_ms and abs(real * 1000 - v.duration_ms) > 100:
            rep.warnings.append(f"{v.video_id}: duration {real:.3f} s differs from session.json "
                                f"({v.duration_ms / 1000:.3f} s)")
        measured = info.video.exact_frame_rate()  # (the working copy's own rate, as refresh_video_facts reads it)
        if not v.rate_known:
            rep.warnings.append(f"{v.video_id}: session.json does not state its frame rate"
                                + (f" (the file is {float(measured):.3f} fps)" if measured else ""))
        elif measured and abs(float(measured) - float(v.exact_fps)) > 0.01:
            rep.warnings.append(f"{v.video_id}: file is {float(measured):.3f} fps but session.json says "
                                f"{float(v.exact_fps):.3f}")
        if measured and not v.frame_rate:
            rates[v.video_id] = measured

    known_steps = {s.number: s.name for s in cfg.skills} if cfg else None
    for i, rec in enumerate(state.annotations, start=1):
        where = f"row {i} (step {rec.step_no})"
        if rec.end_time < rec.start_time:
            rep.errors.append(f"{where}: end_time {rec.end_time} is before start_time {rec.start_time}")
        ts = state.get_video(rec.time_source)
        if ts is None:
            rep.errors.append(f"{where}: time_source '{rec.time_source}' is not a session video")
        else:
            dur, fps = durations.get(ts.video_id), rates.get(ts.video_id)
            if dur and rec.end_time > dur + 1.0 / float(fps or ts.exact_fps):
                rep.errors.append(f"{where}: ends at {rec.end_time:.3f} s, after the end of "
                                  f"{ts.video_id} ({dur:.3f} s)")
            if fps and (rec.start_frame != seconds_to_frames(rec.start_time, fps)
                        or rec.end_frame != seconds_to_frames(rec.end_time, fps)):
                rep.warnings.append(f"{where}: frame numbers do not match times at {float(fps):g} fps (exports follow "
                                    "the times; opening the session in the GUI corrects label.tsv)")
        if state.get_video(rec.audio_source) is None:
            rep.warnings.append(f"{where}: audio_source '{rec.audio_source}' is not a session video")
        for cam in decode_camid(rec.camid):
            if state.get_video(cam) is None:
                rep.warnings.append(f"{where}: camid lists unknown video '{cam}'")
        if known_steps is not None:
            if rec.step_no not in known_steps:
                rep.warnings.append(f"{where}: label number not in config.json")
            elif known_steps[rec.step_no] != rec.step_name:
                rep.warnings.append(f"{where}: name '{rec.step_name}' differs from config.json "
                                    f"('{known_steps[rec.step_no]}')")
    return rep
