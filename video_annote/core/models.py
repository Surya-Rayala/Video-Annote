# video_annote/core/models.py
"""Plain data model: labels, videos, annotations, session and root configuration.

Serialized forms are kept backward compatible: everything written by 0.1.x still loads, and the
fields 0.1.x reads are still written with the same names and meaning.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional

DEFAULT_FPS = 30.0
# what playing and exporting a video needs from its import (everything else stays out of session.json)
MEDIA_KEYS = ("video_start_s", "variable_frame_rate", "audio")
AUDIO_KEYS = ("channels", "channel_layout", "source_channel_layout", "sample_rate", "start_s", "end_s", "dual_mono")


# 0.2 previews saved the import's technical warnings with each video; the ones worth knowing become today's
# plain notes (see media.ingest and media.analysis for their wording)
_PREVIEW_NOTES = [
    (r"steady (\d+) Hz hum", "The audio has a steady {} Hz electrical buzz (left as recorded)."),
    (r"audio is very quiet", "The audio is very quiet."),
    (r"audio reaches full scale", "The audio reaches full scale in places, so it may be clipped."),
    (r"audio track is digital silence", "The audio track is silent."),
    (r"video has gaps \(about \d+ frames, ([\d.]+) s", "About {} s of video is missing from the recording; the "
                                                        "picture holds still there."),
    (r"source has decoding errors", "Parts of the recording are damaged; they may look or sound wrong."),
    (r"source has (\d+) audio tracks", "The recording has {} audio tracks; only the main one was kept."),
    (r"frame rate could not be determined", "The frame rate could not be read, so 30 frames per second were assumed."),
    (r"completeness was not checked", "It could not be checked that the whole recording was imported."),
]


def _preview_notes(warnings: Any) -> List[str]:
    notes: List[str] = []
    for w in warnings if isinstance(warnings, list) else []:
        for pattern, text in _PREVIEW_NOTES:
            m = re.search(pattern, str(w))
            if m:
                value = m.group(1) if m.groups() else ""
                note = text.format(f"{float(value):.1f}" if "." in value else value)
                if note not in notes:
                    notes.append(note)
    return notes


def lean_media(media: Dict[str, Any]) -> Dict[str, Any]:
    """The facts of ``media`` that the app uses (older sessions also stored codecs and audio statistics)."""
    out = {k: media[k] for k in MEDIA_KEYS if k in media}
    if isinstance(out.get("audio"), dict):
        out["audio"] = {k: out["audio"][k] for k in AUDIO_KEYS if k in out["audio"]}
    return out


def normalize_camid(label: str) -> str:
    """Normalize camid/video id strings to a consistent form."""
    return str(label or "").strip().lower()


def encode_camid(video_ids: List[str]) -> str:
    """["video-1", "video-2"] -> "video-1,video-2" (normalized, de-duplicated, order kept)."""
    out: List[str] = []
    for vid in video_ids or []:
        n = normalize_camid(vid)
        if n and n not in out:
            out.append(n)
    return ",".join(out)


def decode_camid(camid: str) -> List[str]:
    return [n for n in (normalize_camid(p) for p in str(camid or "").split(",")) if n]


def clean_text(value: Any) -> str:
    """Single-line text safe for TSV cells (labels, step names)."""
    return " ".join(str(value or "").replace("\t", " ").splitlines()).strip()


@dataclass(frozen=True)
class SkillStep:
    """A label definition (number + name). Called "skill"/"step" in stored files for compatibility."""
    number: int
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return {"number": int(self.number), "name": str(self.name)}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SkillStep":
        number = d["number"]
        if isinstance(number, bool) or (isinstance(number, float) and not number.is_integer()):
            raise ValueError(f"number {number!r} is not a whole number")
        return SkillStep(number=int(number), name=clean_text(d["name"]))


@dataclass
class VideoItem:
    """One video of a session.

    video_id is the logical id used throughout ("video-1", ...); filename is the working copy inside
    the session folder. ``frame_rate`` is the exact rational rate of the working copy (e.g. "25/1");
    ``fps`` is its float value, kept for files written by 0.1.x. ``recorded`` is when the recording
    was made (see media.recorded; empty: not looked up yet); ``notes`` what the import found worth
    knowing, in plain words; ``imported`` when and with which version; ``media`` the facts playing
    and exporting it need (see MEDIA_KEYS)."""
    video_id: str
    filename: str
    source_type: str = "local"  # "local" | "url"
    source: str = ""
    duration_ms: int = 0
    fps: float = DEFAULT_FPS
    frame_rate: str = ""
    frame_count: int = 0
    media: Dict[str, Any] = field(default_factory=dict)
    recorded: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    imported: str = ""

    @property
    def exact_fps(self) -> Fraction:
        try:
            if self.frame_rate:
                rate = Fraction(self.frame_rate)
                if rate > 0:
                    return rate
        except (ValueError, ZeroDivisionError):
            pass
        fps = self.fps if self.fps and self.fps > 0 else DEFAULT_FPS
        return Fraction(fps).limit_denominator(1001)

    @property
    def display_name(self) -> str:
        """The id and the recording it was imported from, for people: "video-1 · cam1.mp4"."""
        name = re.split(r"[\\/]", str(self.source or "").split("?")[0].rstrip("/\\"))[-1]
        return f"{self.video_id} · {name}" if name else self.video_id

    @property
    def rate_known(self) -> bool:
        """False for a video whose frame rate was never measured (fps 0: inferred from its file name
        while ffprobe was unavailable); exact_fps then only assumes DEFAULT_FPS."""
        return bool(self.frame_rate) or (self.fps or 0) > 0

    def to_dict(self) -> Dict[str, Any]:
        """session.json's entry: what people read first, then the facts the app uses."""
        d: Dict[str, Any] = {
            "video_id": self.video_id,
            "filename": self.filename,
            "source": self.source,
            "source_type": self.source_type,
        }
        if self.recorded:
            d["recorded"] = self.recorded
        d["duration_ms"] = int(self.duration_ms)
        d["fps"] = float(self.fps)
        if self.frame_rate:
            d["frame_rate"] = self.frame_rate
        if self.frame_count:
            d["frame_count"] = int(self.frame_count)
        if self.notes:
            d["notes"] = list(self.notes)
        if self.imported:
            d["imported"] = self.imported
        media = lean_media(self.media)
        if media:
            d["media"] = media
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "VideoItem":
        def num(key: str, default: float) -> float:
            try:
                value = float(d.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) else default

        fps = num("fps", DEFAULT_FPS)
        return VideoItem(
            video_id=str(d["video_id"]),
            filename=str(d["filename"]),
            source_type=str(d.get("source_type", "local")),
            source=str(d.get("source", "")),
            duration_ms=int(num("duration_ms", 0)),
            # 0 is stored for a rate that was never measured (0.1.x wrote 30.0 instead), and must stay
            # unknown when read back
            fps=fps if fps > 0 else 0.0,
            frame_rate=str(d.get("frame_rate", "")),
            frame_count=int(num("frame_count", 0)),
            media=dict(d.get("media") or {}),
            recorded=dict(d["recorded"]) if isinstance(d.get("recorded"), dict) else {},
            notes=([str(n) for n in d["notes"]] if isinstance(d.get("notes"), list) else
                   _preview_notes((d.get("ingest") or {}).get("warnings") if isinstance(d.get("ingest"), dict) else None)),
            imported=str(d.get("imported") or (str((d.get("ingest") or {}).get("imported_at") or "")[:16].replace("T", " ")
                                               if isinstance(d.get("ingest"), dict) else "")),
        )


@dataclass
class AnnotationRecord:
    """One labeled interval. Times are seconds on the time-source video's clock; frames are derived
    from the time source's frame rate. Field names and order define the label.tsv contract."""
    label: str
    camid: str
    step_no: int
    step_name: str
    start_frame: int
    end_frame: int
    total_frames: int
    start_time: float
    end_time: float
    total_time: float
    time_source: str
    audio_source: str
    confidence: int = 5
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "camid": self.camid,
            "step_no": int(self.step_no),
            "step_name": self.step_name,
            "start_frame": int(self.start_frame),
            "end_frame": int(self.end_frame),
            "total_frames": int(self.total_frames),
            "start_time": float(self.start_time),
            "end_time": float(self.end_time),
            "total_time": float(self.total_time),
            "time_source": self.time_source,
            "audio_source": self.audio_source,
            "confidence": int(self.confidence),
            "notes": self.notes or "",
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AnnotationRecord":
        """Inverse of to_dict (as in 0.1.x)."""
        return AnnotationRecord(
            label=str(d.get("label", "")),
            camid=str(d.get("camid", "")),
            step_no=int(d.get("step_no", 0)),
            step_name=str(d.get("step_name", "")),
            start_frame=int(d.get("start_frame", 0)),
            end_frame=int(d.get("end_frame", 0)),
            total_frames=int(d.get("total_frames", 0)),
            start_time=float(d.get("start_time", 0.0)),
            end_time=float(d.get("end_time", 0.0)),
            total_time=float(d.get("total_time", 0.0)),
            time_source=str(d.get("time_source", "")),
            audio_source=str(d.get("audio_source", "")),
            confidence=int(d.get("confidence", 5)),
            notes=str(d.get("notes", "")),
        )


@dataclass
class SessionState:
    """In-memory state of the loaded session plus the in-progress label capture."""
    root_dir: Optional[str] = None
    label: Optional[str] = None
    session_dir: Optional[str] = None

    videos: List[VideoItem] = field(default_factory=list)
    annotations: List[AnnotationRecord] = field(default_factory=list)

    active_view_ids: List[str] = field(default_factory=list)
    time_source_id: Optional[str] = None
    audio_source_id: Optional[str] = None

    # content hashes of session.json / label.tsv as last read or written by this state, and the
    # identity of the session folder: used to notice another window or program changing them, or the
    # folder being replaced or moved, in between (see storage.persist_session_state)
    disk_signatures: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    folder_id: Any = field(default=None, repr=False, compare=False)

    pending_step: Optional[SkillStep] = None
    pending_camid: Optional[str] = None
    pending_start_ms: Optional[int] = None
    pending_time_source: Optional[str] = None
    pending_audio_source: Optional[str] = None
    pending_color: Optional[str] = None

    def clear_pending(self) -> None:
        self.pending_step = None
        self.pending_camid = None
        self.pending_start_ms = None
        self.pending_time_source = None
        self.pending_audio_source = None
        self.pending_color = None

    @property
    def is_capturing(self) -> bool:
        return self.pending_step is not None or self.pending_start_ms is not None

    def is_loaded(self) -> bool:
        return bool(self.root_dir and self.label and self.session_dir)

    def video_ids(self) -> List[str]:
        return [v.video_id for v in self.videos]

    def get_video(self, video_id: str) -> Optional[VideoItem]:
        vid = normalize_camid(video_id)
        return next((v for v in self.videos if normalize_camid(v.video_id) == vid), None)

    def ensure_default_sources(self) -> None:
        """Point time/audio sources and the visible views at existing videos."""
        ids = self.video_ids()
        if not ids:
            self.time_source_id = self.audio_source_id = None
            self.active_view_ids = []
            return
        self.active_view_ids = [v for v in self.active_view_ids if v in ids] or [ids[0]]
        if self.time_source_id not in ids:
            self.time_source_id = ids[0]
        if self.audio_source_id not in ids:
            self.audio_source_id = ids[0]


@dataclass
class RootConfig:
    """<root>/config.json: the label list and the stable label -> color index map."""
    root_dir: str
    skills: List[SkillStep] = field(default_factory=list)
    skill_color_map: Dict[int, int] = field(default_factory=dict)
    # not stored: entries of the file that could not be read, the file's content hash and this
    # configuration as last read or written (to notice changes made elsewhere and avoid needless writes)
    problems: List[str] = field(default_factory=list, repr=False, compare=False)
    disk_hash: Optional[str] = field(default=None, repr=False, compare=False)
    saved_state: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "data_root": self.root_dir,
            "skills": [s.to_dict() for s in self.skills],
            "skill_color_map": {str(k): int(v) for k, v in self.skill_color_map.items()},
            "palette_version": 1,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RootConfig":
        """Parse what can be parsed; entries that cannot are listed in ``problems``."""
        problems: List[str] = []
        skills: List[SkillStep] = []
        raw_skills = d.get("skills")
        if raw_skills is not None and not isinstance(raw_skills, list):
            problems.append(f'"skills" is not a list: {str(raw_skills)[:80]}')
        for raw in raw_skills if isinstance(raw_skills, list) else []:
            try:
                skills.append(SkillStep.from_dict(raw))
            except (KeyError, TypeError, ValueError, OverflowError) as e:
                problems.append(f"label {str(raw)[:80]} ({type(e).__name__}: {e})")
        color_map: Dict[int, int] = {}
        raw_map = d.get("skill_color_map")
        if raw_map is not None and not isinstance(raw_map, dict):
            problems.append(f'"skill_color_map" is not an object: {str(raw_map)[:80]}')
        for k, v in (raw_map.items() if isinstance(raw_map, dict) else ()):
            try:
                color_map[int(k)] = int(v)
            except (TypeError, ValueError, OverflowError) as e:
                problems.append(f"color {k}: {str(v)[:40]} ({type(e).__name__}: {e})")
        cfg = RootConfig(
            root_dir=str(d.get("data_root") or d.get("root_dir") or ""),
            skills=skills,
            skill_color_map=color_map,
        )
        cfg.problems = problems
        return cfg
