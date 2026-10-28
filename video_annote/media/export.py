# video_annote/media/export.py
"""Export annotated intervals as ML-ready clips.

For every annotation and every selected camera, the clip contains exactly the frames the annotator
saw: from the frame on screen at the start time to the frame on screen at the end time, inclusive
(frame k of a working copy is shown during [k/fps, (k+1)/fps)). The clip's audio covers exactly
the same span, sample-accurately, so audio and video stay aligned. Optionally a lossless WAV of the
annotation's audio source is written too. A manifest (CSV + JSON) describes every output.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.models import AnnotationRecord, SessionState, VideoItem, decode_camid, normalize_camid
from ..core.timecode import displayed_frame, seconds_to_frames
from .analysis import analyze_audio
from .ffmpeg import CancelCheck, CancelledError, FFmpegError, ffmpeg_version, run_ffmpeg
from .ingest import ALAC_LAYOUTS, alac_layout_filter, audio_span, unknown_layout
from .probe import probe
from .recorded import start_of

SEEK_MARGIN_S = 1.0
CAMERA_MODES = ("annotated", "all", "time_source")
CHANNEL_MODES = ("auto", "mono", "keep")
WAV_CODECS = {"s16": "pcm_s16le", "s24": "pcm_s24le", "f32": "pcm_f32le"}
STAGING_DIRNAME = ".export-in-progress"
FAILED_DIRNAME = "_failed"


@dataclass(frozen=True)
class ExportOptions:
    out_dir: str
    cameras: str = "annotated"  # annotated: the views selected when the label was recorded
    write_video: bool = True
    write_wav: bool = True
    audio_channels: str = "auto"  # auto: mono for mono/dual-mono sources, else keep
    audio_sample_rate: Optional[int] = None  # None keeps the working copy's rate
    wav_format: str = "s16"  # s16 (readable everywhere) | s24 | f32 (also keeps peaks above full scale)
    crf: int = 18
    preset: str = "veryfast"
    workers: int = 2


@dataclass(frozen=True)
class ClipJob:
    record_index: int
    kind: str  # "video" | "wav"
    video_id: str
    source_path: str
    out_path: str
    fps: Fraction
    first_frame: int
    last_frame: int  # inclusive
    has_audio: bool
    audio_filter: str  # applied after the cut (channels, rate)
    video_start_s: float = 0.0  # presentation time of frame 0 (0 for working copies made by 0.2+)
    approximate: bool = False  # variable-frame-rate working copy (0.1.x): cut by time, ±1 frame
    audio_channels: int = 0  # channels the output's audio must have

    @property
    def num_frames(self) -> int:
        return self.last_frame - self.first_frame + 1

    @property
    def start_s(self) -> float:
        return self.video_start_s + float(Fraction(self.first_frame) / self.fps)

    @property
    def end_s(self) -> float:
        return self.video_start_s + float(Fraction(self.last_frame + 1) / self.fps)


@dataclass
class ClipResult:
    job: ClipJob
    ok: bool
    error: str = ""
    duration_s: float = 0.0
    frames: int = 0
    path: str = ""  # where the output is: the dataset file, its _failed/ copy, or "" if none was written


@dataclass
class ExportReport:
    out_dir: str
    manifest_csv: str = ""
    manifest_json: str = ""
    results: List[ClipResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def failed(self) -> List[ClipResult]:
        return [r for r in self.results if not r.ok]


def _safe(text: str) -> str:
    """File-name-safe version of a label. Session labels are already validated as folder names, so
    only characters no file system accepts are replaced; distinct labels stay distinct."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip() or "session"


def _wav_audio_filter(item: VideoItem, channels_mode: str, sample_rate: Optional[int]) -> Tuple[str, int]:
    """(filter, output channels) for a WAV: the requested channels and sample rate."""
    audio = item.media.get("audio") or {}
    channels = int(audio.get("channels") or 0)
    parts: List[str] = []
    if channels >= 2 and (channels_mode == "mono" or (channels_mode == "auto" and audio.get("dual_mono"))):
        if audio.get("dual_mono"):
            parts.append("pan=mono|c0=c0")  # identical channels: taking one is exact
        elif channels == 2:
            parts.append("pan=mono|c0=0.5*c0+0.5*c1")
        elif "source_channel_layout" in audio and unknown_layout(str(audio["source_channel_layout"])):
            # unlabelled channels (e.g. a microphone array) are stored with a layout that may call one of
            # them LFE, which a standard downmix leaves out: give every channel the same weight
            parts.append("pan=mono|c0=" + "+".join(f"{1 / channels:.6f}*c{i}" for i in range(channels)))
        else:
            # standard downmix (keeps centre/surround), normalized so that no output can clip; explicit,
            # because ffmpeg only normalizes an automatic downmix when it outputs integer samples
            parts.append("aresample=rematrix_maxval=1,aformat=channel_layouts=mono")
        channels = 1
    if sample_rate:
        parts.append(f"aresample={int(sample_rate)}")
    return ",".join(parts), channels


def _clip_audio_filter(item: VideoItem) -> Tuple[str, int]:
    """(filter, output channels) for an MP4 clip: the working copy's audio, exactly (an identical
    stereo pair is stored once, as at import). Channels are relabelled, never mixed, to the ALAC
    layout of the same count (ALAC accepts no other layout)."""
    audio = item.media.get("audio") or {}
    channels = int(audio.get("channels") or 0)
    if channels == 2 and audio.get("dual_mono"):
        return "pan=mono|c0=c0", 1
    return alac_layout_filter(channels, str(audio.get("channel_layout") or "")), channels


def _session_identity(state: SessionState) -> str:
    """Identity of a session for its export folder: its label and the recordings it was imported
    from. It stays the same when the data root is copied or moved, and differs for another session
    that happens to have the same label."""
    parts = [state.label or ""] + [f"{v.video_id}|{v.source}|{v.filename}|{v.frame_count}" for v in state.videos]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _label_span(state: SessionState, rec: AnnotationRecord) -> Tuple[float, float]:
    """A label's start and end time as label.tsv defines them: with 3 decimals, on the start of the
    time-source frames start_frame = round(start_time * fps) and end_frame. (Labels made by 0.1.x hold
    times between frames; this keeps their clips on the frames label.tsv lists.)"""
    t0, t1 = round(float(rec.start_time), 3), round(float(rec.end_time), 3)
    item = state.get_video(rec.time_source)
    if item is None:
        return t0, t1
    fps = item.exact_fps
    return (round(float(Fraction(seconds_to_frames(t0, fps)) / fps), 3),
            round(float(Fraction(seconds_to_frames(t1, fps)) / fps), 3))


def _audio_facts(state: SessionState, item: VideoItem) -> Optional[Dict[str, object]]:
    """Audio facts of a working copy (None = no audio). Sessions from 0.1.x lack them, so they are
    measured on demand (one fast decode) and cached on the item."""
    if "audio" in item.media:
        return item.media["audio"]
    path = os.path.join(state.session_dir or "", item.filename)
    info = probe(path)
    facts: Optional[Dict[str, object]] = None
    if info.audio is not None:
        stats = analyze_audio(path, info.audio.channels)
        facts = {"channels": info.audio.channels, "channel_layout": info.audio.channel_layout,
                 "sample_rate": info.audio.sample_rate, **audio_span(info), "dual_mono": bool(stats.dual_mono)}
    item.media["audio"] = facts
    return facts


def _audio_cover(item: VideoItem, audio: Dict[str, object], fps: Fraction) -> Tuple[int, int, float, float]:
    """(first frame, frames end) at ``fps`` from t=0 that a working copy's audio covers, at least half of
    each, plus the audio's start and end in seconds. Frames end is 0 if unknown. (Sessions imported
    before the audio span was stored: from 0 to its video's end.)"""
    start, end = audio.get("start_s"), audio.get("end_s")
    start = float(start) if isinstance(start, (int, float)) else 0.0
    if not isinstance(end, (int, float)) or end <= 0:
        end = float(Fraction(item.frame_count) / item.exact_fps) if item.frame_count else 0.0
    first = max(0, math.ceil(start * float(fps) - 0.5))
    return first, math.floor(float(end) * float(fps) + 0.5), start, float(end)


def plan_clips(state: SessionState, options: ExportOptions) -> Tuple[List[ClipJob], List[str]]:
    """Work list for an export (pure apart from on-demand audio analysis of old sessions)."""
    if options.cameras not in CAMERA_MODES:
        raise ValueError(f"cameras must be one of {CAMERA_MODES}")
    if options.audio_channels not in CHANNEL_MODES:
        raise ValueError(f"audio_channels must be one of {CHANNEL_MODES}")
    if options.wav_format not in WAV_CODECS:
        raise ValueError(f"wav_format must be one of {tuple(WAV_CODECS)}")
    session = _safe(state.label or "session")
    jobs: List[ClipJob] = []
    warnings: List[str] = []
    facts: Dict[str, Optional[Dict[str, object]]] = {}
    for item in state.videos:
        try:
            facts[item.video_id] = _audio_facts(state, item)
        except FFmpegError as e:  # (0.1.x sessions: measured now) only this video's outputs are lost
            warnings.append(f"{item.video_id}: {item.filename} could not be read ({e.summary}); its clips and WAVs "
                            "are skipped")
    for item in state.videos:
        if item.media.get("variable_frame_rate"):
            warnings.append(f"{item.video_id} has a variable frame rate: its clips are cut by time and their "
                            "frame numbers are approximate (for exact frames, see Limitations in the README)")

    for i, rec in enumerate(state.annotations):
        if rec.end_time < rec.start_time:
            warnings.append(f"row {i + 1}: end before start, skipped")
            continue
        cams = _cameras(state, options, rec)
        stem = os.path.join(options.out_dir, session, f"{session}_{i + 1:04d}_step{int(rec.step_no):03d}")
        targets = [("video", cam) for cam in cams] if options.write_video else []
        if options.write_wav:
            targets.append(("wav", rec.audio_source or rec.time_source))
        for kind, video_id in targets:
            job = _clip_job(state, options, facts, i, rec, kind, video_id, stem, warnings)
            if job is not None:
                jobs.append(job)
    return jobs, warnings


def _cameras(state: SessionState, options: ExportOptions, rec: AnnotationRecord) -> List[str]:
    """The videos a label's MP4 clips are cut from."""
    if options.cameras == "all":
        return state.video_ids()
    if options.cameras == "time_source":
        return [rec.time_source]
    return decode_camid(rec.camid) or [rec.time_source]


def left_out_outputs(state: SessionState, options: ExportOptions) -> Dict[str, int]:
    """Files of the previous export in this export's folder that ``options`` leave out (an output type,
    or cameras), counted by kind ("video", "wav"). An export keeps its folder matching its manifest, so
    they are removed when it finishes; callers can ask first. Files of deleted labels are not counted."""
    folder = os.path.join(options.out_dir, _safe(state.label or "session"))
    clips = _read_manifest(folder).get("clips")
    counts: Dict[str, int] = {}
    for clip in clips if isinstance(clips, list) else []:
        if not isinstance(clip, dict) or not clip.get("file"):
            continue
        try:
            row = int(clip.get("row"))
        except (TypeError, ValueError):
            continue
        if not 1 <= row <= len(state.annotations) or not os.path.isfile(os.path.join(folder, str(clip["file"]))):
            continue
        kind = clip.get("kind")
        if kind == "wav":
            kept = options.write_wav
        elif kind == "video":
            cams = {normalize_camid(c) for c in _cameras(state, options, state.annotations[row - 1])}
            kept = options.write_video and normalize_camid(str(clip.get("video_id", ""))) in cams
        else:
            continue
        if not kept:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _clip_job(state: SessionState, options: ExportOptions, facts: Dict[str, Optional[Dict[str, object]]],
              row: int, rec: AnnotationRecord, kind: str, video_id: str, stem: str,
              warnings: List[str]) -> Optional[ClipJob]:
    item = state.get_video(video_id)
    if item is None:
        warnings.append(f"row {row + 1}: video '{video_id}' is not part of the session, skipped")
        return None
    if item.video_id not in facts:  # unreadable working copy (said once, in plan_clips)
        return None
    fps = item.exact_fps
    vstart = float(item.media.get("video_start_s") or 0.0)
    t0, t1 = _label_span(state, rec)
    ts_item = state.get_video(rec.time_source)
    if kind == "wav" and ts_item is not None:
        # audio is not cut in frames: take exactly the label's span, the time-source frames
        # start_frame..end_frame (the audio source may run at another frame rate)
        fps, vstart = ts_item.exact_fps, 0.0
    first = displayed_frame(max(0.0, t0 - vstart), fps)
    last = max(first, displayed_frame(max(0.0, t1 - vstart), fps))
    audio = facts.get(item.video_id)
    if kind == "wav" and audio is None:
        warnings.append(f"row {row + 1}: {item.video_id} has no audio, WAV skipped")
        return None
    if kind != "wav":
        total = item.frame_count
        if total and last >= total:
            if first >= total:
                warnings.append(f"row {row + 1}: {item.video_id} ends before this annotation starts, skipped")
                return None
            warnings.append(f"row {row + 1}: {item.video_id} ends early; clip truncated to frame {total - 1}")
            last = total - 1
    else:
        # the WAV holds only recorded audio: it keeps to the frames the audio source's audio covers (and
        # to the time source's frames) instead of padding the label with silence
        def at(frame: int) -> str:
            return f"{float(Fraction(frame) / fps):.3f} s"

        a_first, a_end, a_start_s, a_end_s = _audio_cover(item, audio or {}, fps)
        ends = [(a_end, f"the audio of {item.video_id} ends at {a_end_s:.3f} s")] if a_end else []
        if ts_item is not None and ts_item.frame_count:
            ends.append((ts_item.frame_count, f"{ts_item.video_id} (the time source) ends at {at(ts_item.frame_count)}"))
        end, ender = min(ends) if ends else (0, "")  # whichever ends first
        starter = f"the audio of {item.video_id} starts at {a_start_s:.3f} s"
        new_first, new_last = max(first, a_first), min(last, end - 1) if end else last
        if new_first > new_last:  # no part of the label has audio
            why = (f"{ender}, before this annotation starts" if end and first >= end else
                   f"{starter}, after this annotation ends" if last < a_first else f"{starter} and {ender}")
            warnings.append(f"row {row + 1}: {why}; WAV skipped")
            return None
        if new_first > first:
            warnings.append(f"row {row + 1}: {starter}; WAV starts at {at(new_first)}")
        if new_last < last:
            warnings.append(f"row {row + 1}: {ender}; WAV truncated at {at(new_last + 1)}")
        first, last = new_first, new_last
    suffix = "audio.wav" if kind == "wav" else f"{normalize_camid(item.video_id)}.mp4"
    has_audio = audio is not None
    afilter, channels = "", 0
    if kind == "wav":
        afilter, channels = _wav_audio_filter(item, options.audio_channels, options.audio_sample_rate)
    elif has_audio:
        try:
            afilter, channels = _clip_audio_filter(item)
        except ValueError:
            n = int((audio or {}).get("channels") or 0)
            warnings.append(f"row {row + 1}: {item.video_id} has {n} audio channels; MP4 clips can carry at most "
                            f"{max(ALAC_LAYOUTS)} losslessly, so its clip has no audio (the WAV has all of them)")
            afilter, channels, has_audio = "", 0, False
    return ClipJob(
        record_index=row, kind=kind, video_id=item.video_id,
        source_path=os.path.join(state.session_dir or "", item.filename),
        out_path=f"{stem}_{suffix}",
        fps=fps, first_frame=first, last_frame=last, video_start_s=vstart,
        approximate=bool(item.media.get("variable_frame_rate")), has_audio=has_audio,
        audio_filter=afilter, audio_channels=channels,
    )


def clip_command(job: ClipJob, options: ExportOptions) -> List[str]:
    """ffmpeg arguments: fast-seek to a point before the clip, then trim exactly.

    After an input seek, timestamps are relative to the seek point. Video is cut with mid-frame
    bounds, which select frames first..last regardless of rounding; audio is cut at the exact
    frame-boundary times."""
    pre = max(0.0, job.start_s - SEEK_MARGIN_S)
    frame = 1.0 / float(job.fps)
    v0 = job.video_start_s + (job.first_frame - 0.5) * frame - pre
    v1 = job.video_start_s + (job.last_frame + 0.5) * frame - pre
    a0, a1 = job.start_s - pre, job.end_s - pre
    args = ["-ss", f"{pre:.6f}", "-i", job.source_path]
    # Lay the audio on the timeline first (silence where the track starts late, padding where it ends
    # early), so that the cut stays aligned with the frames whatever the track's own start/end.
    afilter = (f"aresample=async=1:first_pts=0,atrim=start={a0:.6f}:end={a1:.6f},asetpts=PTS-STARTPTS,"
               f"apad=whole_dur={job.end_s - job.start_s:.6f}")
    if job.audio_filter:
        afilter += "," + job.audio_filter
    if job.kind == "wav":
        if options.wav_format != "f32":
            # convert via packed float: ffmpeg's planar-float -> integer path truncates (a -0.5 LSB bias
            # on stereo), the packed path rounds correctly
            afilter += ",aformat=sample_fmts=flt"
        return args + ["-map", "0:a:0", "-vn", "-af", afilter, "-c:a", WAV_CODECS[options.wav_format],
                       "-f", "wav", job.out_path]
    graph = f"[0:v:0]trim=start={max(v0, 0):.6f}:end={v1:.6f},setpts=PTS-STARTPTS[v]"
    out = ["-map", "[v]"]
    if job.has_audio:
        graph += f";[0:a:0]{afilter}[a]"
        # lossless audio in the clip too (24-bit ALAC): no second lossy generation
        out += ["-map", "[a]", "-c:a", "alac", "-sample_fmt", "s32p"]
    return args + [
        "-filter_complex", graph, *out,
        "-c:v", "libx264", "-preset", options.preset, "-crf", str(options.crf), "-pix_fmt", "yuv420p",
        # explicit constant rate: the working copy already is CFR so frames map 1:1, and every packet
        # (including the last) gets a duration; with passthrough, ffmpeg 7.x writes the last frame with
        # duration 0 and players drop it
        "-fps_mode", "cfr", "-r", f"{job.fps.numerator}/{job.fps.denominator}",
        "-movflags", "+faststart", "-f", "mp4", job.out_path,
    ]


def _verify(job: ClipJob, path: str) -> Tuple[bool, str, float, int]:
    info = probe(path)
    if job.has_audio and (info.audio.channels if info.audio else 0) != job.audio_channels:
        return False, (f"audio has {info.audio.channels if info.audio else 0} channels, "
                       f"expected {job.audio_channels}"), 0.0, 0
    if job.kind == "wav":
        dur = (info.audio.duration if info.audio else None) or info.duration or 0.0
        expected = job.num_frames / float(job.fps)
        ok = abs(dur - expected) <= 0.002
        return ok, "" if ok else f"WAV lasts {dur:.4f} s, expected {expected:.4f} s", dur, 0
    frames = info.video.frame_count if info.video else 0
    dur = info.video_duration or 0.0
    slack = 1 if job.approximate else 0  # a variable-rate copy's frames do not sit on the nominal grid
    expected = (frames if slack and frames else job.num_frames) / float(job.fps)
    if abs((frames or 0) - job.num_frames) > slack:
        return False, f"clip has {frames} frames, expected {job.num_frames}", dur, frames or 0
    if abs(dur - expected) > 0.5 / float(job.fps):  # e.g. a last frame with zero duration
        return False, f"clip video lasts {dur:.3f} s, expected {expected:.3f} s", dur, frames
    if job.has_audio:
        adur = (info.audio.duration or 0.0) if info.audio else 0.0
        if abs(adur - job.num_frames / float(job.fps)) > 0.03 + slack / float(job.fps):
            return False, f"clip audio lasts {adur:.3f} s, video {dur:.3f} s", dur, frames
    return True, "", dur, frames


def export_segments(
    state: SessionState,
    options: ExportOptions,
    on_progress: Optional[Callable[[float, str], None]] = None,
    should_cancel: Optional[CancelCheck] = None,
) -> ExportReport:
    from ..sessions import frame_mismatch_note, refresh_video_facts, sync_label_frames

    state = copy.deepcopy(state)  # never mutate the caller's (UI's) objects from a worker thread
    notes = refresh_video_facts(state)
    rows = sync_label_frames(state)  # (only for the manifest: clips are cut from the label times anyway)
    if rows:
        notes.append(frame_mismatch_note(rows, "the clips follow the times (opening the session in the GUI corrects "
                                               "label.tsv)"))
    jobs, warnings = plan_clips(state, options)
    report = ExportReport(out_dir=options.out_dir, warnings=notes + warnings)
    if not jobs:
        return report
    folder = os.path.dirname(jobs[0].out_path)
    os.makedirs(folder, exist_ok=True)
    manifest = _read_manifest(folder)
    _check_owner(folder, manifest, state)
    # Outputs are written to a staging folder and only moved into place once every job has run, so a
    # cancelled or interrupted export leaves the previous export and its manifest exactly as they were.
    staging = os.path.join(folder, STAGING_DIRNAME)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    done = 0

    def run(job: ClipJob) -> ClipResult:
        if should_cancel is not None and should_cancel():
            raise CancelledError()
        staged = os.path.join(staging, os.path.basename(job.out_path))
        try:
            cmd = clip_command(job, options)
            cmd[-1] = staged
            run_ffmpeg(cmd, should_cancel=should_cancel)
            ok, err, dur, frames = _verify(job, staged)
            return ClipResult(job, ok, err, dur, frames)
        except FFmpegError as e:
            return ClipResult(job, False, str(e).splitlines()[0])

    try:
        with ThreadPoolExecutor(max_workers=max(1, options.workers)) as pool:
            for result in pool.map(run, jobs):
                report.results.append(result)
                done += 1
                if on_progress is not None:
                    on_progress(done / len(jobs), os.path.basename(result.job.out_path))
        _publish(folder, staging, manifest, report)
        _write_manifest(state, options, report)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return report


def _publish(folder: str, staging: str, manifest: Dict[str, Any], report: ExportReport) -> None:
    """Move a finished export into place. Verified outputs replace their previous versions; failed
    ones go to _failed/ with a ".failed" suffix (kept for inspection, invisible to *.mp4/*.wav globs)
    and never leave an older version behind; files of the previous export that this one did not
    produce (deleted or renumbered labels) are removed. Only files listed in the old manifest and
    files of this export are touched."""
    for name in ("manifest.csv", "manifest.json"):  # no manifest until the new one is complete
        if os.path.isfile(os.path.join(folder, name)):
            os.remove(os.path.join(folder, name))
    failed_dir = os.path.join(folder, FAILED_DIRNAME)
    shutil.rmtree(failed_dir, ignore_errors=True)  # failures of an earlier export are obsolete
    for r in report.results:
        staged = os.path.join(staging, os.path.basename(r.job.out_path))
        if r.ok:
            os.replace(staged, r.job.out_path)
            r.path = r.job.out_path
            continue
        if os.path.exists(r.job.out_path):
            os.remove(r.job.out_path)
        if os.path.exists(staged):
            os.makedirs(failed_dir, exist_ok=True)
            r.path = os.path.join(failed_dir, os.path.basename(r.job.out_path) + ".failed")
            os.replace(staged, r.path)
    current = {os.path.abspath(r.job.out_path) for r in report.results}
    for path in _previous_outputs(folder, manifest) - current:
        if os.path.isfile(path):
            os.remove(path)


def _read_manifest(folder: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _check_owner(folder: str, manifest: Dict[str, Any], state: SessionState) -> None:
    """An export folder belongs to one session: another session's files (a different label, or the
    same label imported from other recordings) must not be overwritten or cleaned up."""
    label, ident = manifest.get("session"), manifest.get("session_id")
    if (label is not None and label != state.label) or (ident is not None and ident != _session_identity(state)):
        raise ValueError(f"{folder} holds the export of another session ('{label}', made from other recordings "
                         f"in {manifest.get('session_dir') or 'another data root'}); choose another output folder, "
                         "or delete that export first.")


def _previous_outputs(folder: str, manifest: Dict[str, Any]) -> set:
    clips = manifest.get("clips")
    root = os.path.abspath(folder)
    out = set()
    for clip in clips if isinstance(clips, list) else []:
        if not isinstance(clip, dict):
            continue
        path = os.path.abspath(os.path.join(root, str(clip.get("file", ""))))
        if os.path.dirname(path) == root:  # never anything outside the export folder
            out.add(path)
    return out


MANIFEST_FIELDS = [
    "session", "row", "step_no", "step_name", "kind", "video_id", "file",
    "first_frame", "last_frame", "num_frames", "fps", "clip_start_s", "clip_end_s", "clip_start_unix", "clip_end_unix",
    "label_start_time", "label_end_time", "time_source", "audio_source", "camid",
    "confidence", "notes", "ok", "error",
]


def _manifest_row(state: SessionState, rec: AnnotationRecord, r: ClipResult, root: str) -> Dict[str, object]:
    j = r.job
    item = state.get_video(j.video_id)
    recorded = start_of(item.recorded) if item is not None else None  # real-world time of that video's t=0

    def unix(seconds: float) -> object:
        return round(recorded.timestamp() + seconds, 6) if recorded is not None else ""

    return {
        "session": state.label, "row": j.record_index + 1, "step_no": rec.step_no, "step_name": rec.step_name,
        "kind": j.kind, "video_id": j.video_id, "file": os.path.relpath(r.path, root) if r.path else "",
        "first_frame": j.first_frame, "last_frame": j.last_frame,
        "num_frames": r.frames if j.approximate and r.frames else j.num_frames,
        "fps": f"{j.fps.numerator}/{j.fps.denominator}", "clip_start_s": round(j.start_s, 6),
        "clip_end_s": round(j.end_s, 6), "clip_start_unix": unix(j.start_s), "clip_end_unix": unix(j.end_s),
        "label_start_time": rec.start_time, "label_end_time": rec.end_time,
        "time_source": rec.time_source, "audio_source": rec.audio_source, "camid": rec.camid,
        "confidence": rec.confidence, "notes": rec.notes, "ok": r.ok, "error": r.error,
    }


def _write_manifest(state: SessionState, options: ExportOptions, report: ExportReport) -> None:
    folder = os.path.join(options.out_dir, _safe(state.label or "session"))
    rows = [_manifest_row(state, state.annotations[r.job.record_index], r, folder) for r in report.results]
    report.manifest_csv = os.path.join(folder, "manifest.csv")
    with open(report.manifest_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    report.manifest_json = os.path.join(folder, "manifest.json")
    from .. import __version__
    with open(report.manifest_json, "w", encoding="utf-8") as f:
        json.dump({
            "session": state.label,
            "session_id": _session_identity(state),
            "session_dir": os.path.abspath(state.session_dir or ""),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": f"video-annote {__version__}",
            "ffmpeg": ffmpeg_version(),
            "frame_convention": "frames first_frame..last_frame inclusive; frame k spans [k/fps, (k+1)/fps)",
            "options": {k: v for k, v in asdict(options).items() if k != "out_dir"},
            "videos": {v.video_id: {"filename": v.filename, "frame_rate": str(v.exact_fps),
                                    "audio": v.media.get("audio"), "recorded": v.recorded or None}
                       for v in state.videos},
            "warnings": report.warnings,
            "clips": rows,
        }, f, indent=2, ensure_ascii=False, default=str)
