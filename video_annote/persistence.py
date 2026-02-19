# video_annote/persistence.py
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .domain import (
    AnnotationRecord,
    RootConfig,
    SessionState,
    SkillStep,
    VideoItem,
)


# Filenames (within root/session dirs)
ROOT_CONFIG_FILENAME = "config.json"
SESSION_META_FILENAME = "session.json"
TSV_FILENAME = "label.tsv"


# -----------------------------
# Atomic file helpers
# -----------------------------

def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _atomic_write_json(path: str, payload: Dict) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    _atomic_write_text(path, text + "\n")


def _read_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# -----------------------------
# Root config (root/config.json)
# -----------------------------

def root_config_path(root_dir: str) -> str:
    return os.path.join(root_dir, ROOT_CONFIG_FILENAME)


def load_root_config(root_dir: str) -> Optional[RootConfig]:
    """
    Loads <root_dir>/config.json.

    If missing or invalid, returns None (caller should treat as "blank").
    """
    if not root_dir:
        return None
    path = root_config_path(root_dir)
    if not os.path.exists(path):
        return None
    try:
        data = _read_json(path)
        cfg = RootConfig.from_dict(data)
        # Ensure correct root_dir is used
        if cfg.root_dir != root_dir:
            cfg.root_dir = root_dir
        return cfg
    except Exception:
        return None


def save_root_config(cfg: RootConfig) -> None:
    """
    Saves root config to <root_dir>/config.json atomically.
    """
    if not cfg.root_dir:
        raise ValueError("RootConfig.root_dir is required")
    path = root_config_path(cfg.root_dir)
    _atomic_write_json(path, cfg.to_dict())


# -----------------------------
# Session helpers
# -----------------------------

def session_dir(root_dir: str, label: str) -> str:
    return os.path.join(root_dir, label)


def session_meta_path(session_dir_path: str) -> str:
    return os.path.join(session_dir_path, SESSION_META_FILENAME)


def tsv_path(session_dir_path: str) -> str:
    return os.path.join(session_dir_path, TSV_FILENAME)


def list_sessions(root_dir: str) -> List[str]:
    """
    Returns session folder names within root. Filters out obvious non-session entries.
    """
    if not root_dir or not os.path.isdir(root_dir):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(root_dir)):
        if name.startswith("."):
            continue
        p = os.path.join(root_dir, name)
        if not os.path.isdir(p):
            continue
        # Heuristic: session dir contains session.json or label.tsv or video-1.mp4 etc.
        if (
            os.path.exists(os.path.join(p, SESSION_META_FILENAME))
            or os.path.exists(os.path.join(p, TSV_FILENAME))
        ):
            out.append(name)
            continue
        # fallback: any file "video-*.mp4"
        try:
            for fn in os.listdir(p):
                if fn.lower().startswith("video-") and fn.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".m4v")):
                    out.append(name)
                    break
        except Exception:
            pass
    return out


# -----------------------------
# Session metadata (session/session.json)
# -----------------------------

def build_session_meta(label: str, videos: List[VideoItem]) -> Dict:
    return {
        "label": label,
        "videos": [v.to_dict() for v in videos],
        "meta_version": 1,
    }


def save_session_meta(session_dir_path: str, label: str, videos: List[VideoItem]) -> None:
    path = session_meta_path(session_dir_path)
    _atomic_write_json(path, build_session_meta(label, videos))


def load_session_meta(session_dir_path: str) -> Optional[Dict]:
    path = session_meta_path(session_dir_path)
    if not os.path.exists(path):
        return None
    try:
        return _read_json(path)
    except Exception:
        return None


def load_videos_from_session(session_dir_path: str) -> List[VideoItem]:
    """
    Prefer session.json. If missing, infer from video-* files.
    """
    meta = load_session_meta(session_dir_path)
    if meta and isinstance(meta.get("videos"), list):
        vids: List[VideoItem] = []
        for v in meta["videos"]:
            try:
                vids.append(VideoItem.from_dict(v))
            except Exception:
                continue
        # Ensure files exist (keep items even if missing; UI may show warnings)
        return vids

    # Infer from files
    if not os.path.isdir(session_dir_path):
        return []
    files = sorted(os.listdir(session_dir_path))
    video_files = [f for f in files if f.lower().startswith("video-") and f.lower().endswith(
        (".mp4", ".mov", ".mkv", ".avi", ".m4v")
    )]
    vids = []
    for f in video_files:
        base = os.path.splitext(f)[0]  # "video-1"
        vids.append(VideoItem(video_id=base, filename=f, source_type="local", source=""))
    return vids


# -----------------------------
# TSV read/write
# -----------------------------

TSV_HEADER = [
    "label", "camid", "step_no", "step_name",
    "start_frame", "end_frame", "total_frames",
    "start_time", "end_time", "total_time",
    "time_source", "audio_source", "confidence", "notes",
]


def _escape_notes(notes: str) -> str:
    if notes is None:
        notes = ""
    # keep TSV single-line; preserve \n as literal
    return str(notes).replace("\r", "").replace("\t", " ").replace("\n", "\\n")


def _unescape_notes(notes: str) -> str:
    if notes is None:
        return ""
    # convert literal \n back to newline for UI
    return str(notes).replace("\\n", "\n")


def save_annotations_tsv(session_dir_path: str, annotations: List[AnnotationRecord]) -> str:
    """
    Autosave-friendly TSV writer. Returns the written path.
    """
    path = tsv_path(session_dir_path)
    lines: List[str] = []
    lines.append("\t".join(TSV_HEADER))
    for rec in annotations:
        row = [
            rec.label,
            rec.camid,
            str(int(rec.step_no)),
            rec.step_name,
            str(int(rec.start_frame)),
            str(int(rec.end_frame)),
            str(int(rec.total_frames)),
            f"{float(rec.start_time):.3f}",
            f"{float(rec.end_time):.3f}",
            f"{float(rec.total_time):.3f}",
            rec.time_source,
            rec.audio_source,
            str(int(rec.confidence)),
            _escape_notes(rec.notes),
        ]
        lines.append("\t".join(row))
    _atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def load_annotations_tsv(session_dir_path: str) -> List[AnnotationRecord]:
    path = tsv_path(session_dir_path)
    if not os.path.exists(path):
        return []
    out: List[AnnotationRecord] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        raw = f.read().splitlines()

    if not raw:
        return []

    # Detect header
    start_idx = 0
    first = raw[0].strip()
    if first.startswith("label\t") or first == "\t".join(TSV_HEADER):
        start_idx = 1

    for line in raw[start_idx:]:
        if not line.strip():
            continue
        parts = line.split("\t")
        # tolerate missing trailing columns
        while len(parts) < len(TSV_HEADER):
            parts.append("")
        d = dict(zip(TSV_HEADER, parts[:len(TSV_HEADER)]))

        try:
            rec = AnnotationRecord(
                label=str(d.get("label", "")),
                camid=str(d.get("camid", "")),
                step_no=int(d.get("step_no", 0) or 0),
                step_name=str(d.get("step_name", "")),
                start_frame=int(d.get("start_frame", 0) or 0),
                end_frame=int(d.get("end_frame", 0) or 0),
                total_frames=int(d.get("total_frames", 0) or 0),
                start_time=float(d.get("start_time", 0.0) or 0.0),
                end_time=float(d.get("end_time", 0.0) or 0.0),
                total_time=float(d.get("total_time", 0.0) or 0.0),
                time_source=str(d.get("time_source", "")),
                audio_source=str(d.get("audio_source", "")),
                confidence=int(d.get("confidence", 5) or 5),
                notes=_unescape_notes(d.get("notes", "")),
            )
            out.append(rec)
        except Exception:
            # skip malformed lines
            continue

    return out


# -----------------------------
# High-level load/save session state
# -----------------------------

def load_session_state(root_dir: str, label: str) -> SessionState:
    """
    Loads videos (from session.json or inference) and annotations (from TSV).
    Does not require root config, but root config is typically used elsewhere.
    """
    sdir = session_dir(root_dir, label)
    state = SessionState(root_dir=root_dir, label=label, session_dir=sdir)
    state.videos = load_videos_from_session(sdir)
    state.annotations = load_annotations_tsv(sdir)
    state.ensure_default_sources()
    return state


def persist_session_state(state: SessionState) -> None:
    """
    Writes session.json + label.tsv atomically.
    """
    if not state.session_dir or not state.label:
        raise ValueError("SessionState must have session_dir and label to persist")
    os.makedirs(state.session_dir, exist_ok=True)
    save_session_meta(state.session_dir, state.label, state.videos)
    save_annotations_tsv(state.session_dir, state.annotations)


# -----------------------------
# Validation helpers used by dialogs
# -----------------------------

def session_exists(root_dir: str, label: str) -> Tuple[bool, str]:
    """
    Returns (exists, path).
    """
    p = session_dir(root_dir, label)
    return (os.path.exists(p), p)


def validate_importable_session(root_dir: str, label: str) -> Tuple[bool, str]:
    """
    Validate that a session can be imported:
      - session dir exists
      - has at least one video-*.*
      - has label.tsv (preferred) OR can still load empty annotations
      - root config is optional but recommended; caller may warn
    Returns (ok, message).
    """
    if not root_dir or not os.path.isdir(root_dir):
        return (False, "Root directory is not set or does not exist.")
    sdir = session_dir(root_dir, label)
    if not os.path.isdir(sdir):
        return (False, f"Session folder does not exist: {sdir}")

    vids = load_videos_from_session(sdir)
    if not vids:
        return (False, "No videos found (expected files named like video-1.mp4, video-2.mp4, ...).")

    # TSV can be missing (start fresh), but import should still work
    return (True, "OK")