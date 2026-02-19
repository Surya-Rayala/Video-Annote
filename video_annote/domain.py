# video_annote/domain.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Iterable


# -----------------------------
# Skills / Colors
# -----------------------------

# 50 distinct, high-contrast colors (hex). Assigned sequentially.
SKILL_COLOR_PALETTE_50: List[str] = [
    "#E6194B", "#3CB44B", "#FFE119", "#4363D8", "#F58231",
    "#911EB4", "#46F0F0", "#F032E6", "#BCF60C", "#FABEBE",
    "#008080", "#E6BEFF", "#9A6324", "#FFFAC8", "#800000",
    "#AAFFC3", "#808000", "#FFD8B1", "#000075", "#808080",
    "#000000", "#FF4500", "#1E90FF", "#32CD32", "#FFD700",
    "#8A2BE2", "#00CED1", "#FF1493", "#7FFF00", "#FFB6C1",
    "#20B2AA", "#BA55D3", "#B8860B", "#F0E68C", "#A52A2A",
    "#2E8B57", "#BDB76B", "#D2691E", "#4169E1", "#DC143C",
    "#00FA9A", "#9400D3", "#FF8C00", "#2F4F4F", "#ADFF2F",
    "#C71585", "#00BFFF", "#228B22", "#FF6347", "#6A5ACD",
]

# -----------------------------
# Extra color generation (for >50 skills)
# -----------------------------

# Cache palette metrics for fast "avoid similarity" checks.
# We intentionally ignore low-saturation palette colors (e.g., black/gray) in hue checks.
_PALETTE_HUES_DEG: Optional[List[float]] = None
_PALETTE_RGB: Optional[List[Tuple[int, int, int]]] = None


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    s = (hex_color or "").strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        return (0, 0, 0)
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (r, g, b)
    except Exception:
        return (0, 0, 0)


def _rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    r, g, b = rgb
    r = max(0, min(int(r), 255))
    g = max(0, min(int(g), 255))
    b = max(0, min(int(b), 255))
    return f"#{r:02X}{g:02X}{b:02X}"


def _rgb_to_hsv(r: int, g: int, b: int) -> Tuple[float, float, float]:
    # r,g,b in 0..255 -> h in degrees 0..360, s,v in 0..1
    rf = max(0.0, min(float(r) / 255.0, 1.0))
    gf = max(0.0, min(float(g) / 255.0, 1.0))
    bf = max(0.0, min(float(b) / 255.0, 1.0))

    mx = max(rf, gf, bf)
    mn = min(rf, gf, bf)
    diff = mx - mn

    # Hue
    if diff <= 1e-12:
        h = 0.0
    elif mx == rf:
        h = (60.0 * ((gf - bf) / diff) + 360.0) % 360.0
    elif mx == gf:
        h = (60.0 * ((bf - rf) / diff) + 120.0) % 360.0
    else:
        h = (60.0 * ((rf - gf) / diff) + 240.0) % 360.0

    # Saturation
    s = 0.0 if mx <= 1e-12 else (diff / mx)
    v = mx
    return (h, s, v)


def _hsv_to_rgb(h: float, s: float, v: float) -> Tuple[int, int, int]:
    # h in degrees 0..360, s,v in 0..1 -> r,g,b in 0..255
    h = float(h) % 360.0
    s = max(0.0, min(float(s), 1.0))
    v = max(0.0, min(float(v), 1.0))

    c = v * s
    x = c * (1.0 - abs(((h / 60.0) % 2.0) - 1.0))
    m = v - c

    if 0.0 <= h < 60.0:
        rp, gp, bp = (c, x, 0.0)
    elif 60.0 <= h < 120.0:
        rp, gp, bp = (x, c, 0.0)
    elif 120.0 <= h < 180.0:
        rp, gp, bp = (0.0, c, x)
    elif 180.0 <= h < 240.0:
        rp, gp, bp = (0.0, x, c)
    elif 240.0 <= h < 300.0:
        rp, gp, bp = (x, 0.0, c)
    else:
        rp, gp, bp = (c, 0.0, x)

    r = int(round((rp + m) * 255.0))
    g = int(round((gp + m) * 255.0))
    b = int(round((bp + m) * 255.0))
    return (max(0, min(r, 255)), max(0, min(g, 255)), max(0, min(b, 255)))


def _circular_hue_distance_deg(a: float, b: float) -> float:
    d = abs(float(a) - float(b)) % 360.0
    return min(d, 360.0 - d)


def _get_palette_metrics() -> Tuple[List[float], List[Tuple[int, int, int]]]:
    global _PALETTE_HUES_DEG, _PALETTE_RGB
    if _PALETTE_HUES_DEG is not None and _PALETTE_RGB is not None:
        return _PALETTE_HUES_DEG, _PALETTE_RGB

    rgbs: List[Tuple[int, int, int]] = []
    hues: List[float] = []

    for hx in SKILL_COLOR_PALETTE_50:
        rgb = _hex_to_rgb(hx)
        rgbs.append(rgb)
        h, s, _v = _rgb_to_hsv(*rgb)
        # Ignore near-grayscale colors for hue-based avoidance.
        if s >= 0.18:
            hues.append(float(h))

    _PALETTE_RGB = rgbs
    _PALETTE_HUES_DEG = hues
    return hues, rgbs


def _min_rgb_distance(rgb: Tuple[int, int, int], others: Iterable[Tuple[int, int, int]]) -> float:
    r, g, b = rgb
    best = float("inf")
    for (r2, g2, b2) in others:
        dr = float(r - r2)
        dg = float(g - g2)
        db = float(b - b2)
        d = (dr * dr + dg * dg + db * db) ** 0.5
        if d < best:
            best = d
    return best if best != float("inf") else 0.0


def _generated_color_hex(gen_index: int) -> str:
    """Generate a vivid, deterministic color for indices >= 0.

    Requirements:
      - Spread hues well (golden-angle stepping)
      - Avoid being too close in hue to the first 50 palette colors
      - Avoid being too close in RGB distance to the first 50 palette colors

    Notes:
      - This is deterministic: same gen_index -> same result.
      - We keep S/V high so it won't look like the palette's black/gray.
    """
    base_hues, base_rgbs = _get_palette_metrics()

    # Golden angle (degrees) produces good dispersion on a circle.
    golden = 137.50776405003785

    # "No hue similar" to the first 50: enforce a reasonably large minimum.
    # If we can't satisfy it (unlikely), we relax slightly.
    min_hue = 18.0
    min_rgb = 85.0

    # Start from an offset so generated hues don't accidentally line up with palette ordering.
    base = (float(gen_index) + 1.0) * golden

    best_rgb: Tuple[int, int, int] = (0, 180, 0)
    best_score = -1.0

    for attempt in range(0, 220):
        # Vary hue with golden step; vary S/V in a small cycle for extra separation.
        hue = (base + attempt * (golden / 3.0)) % 360.0

        # Small deterministic variation (keeps colors vivid but not identical).
        sat_cycle = (gen_index + attempt * 7) % 3
        val_cycle = (gen_index + attempt * 11) % 3
        s = 0.74 + 0.08 * (sat_cycle / 2.0)   # 0.74 .. 0.82
        v = 0.88 + 0.07 * (val_cycle / 2.0)   # 0.88 .. 0.95

        rgb = _hsv_to_rgb(hue, s, v)

        # Hue distance vs palette hues (ignore grayscale palette entries).
        if base_hues:
            hue_dist = min(_circular_hue_distance_deg(hue, h2) for h2 in base_hues)
        else:
            hue_dist = 360.0

        # RGB distance vs all palette colors.
        rgb_dist = _min_rgb_distance(rgb, base_rgbs)

        # Hard accept if it clears both thresholds.
        if hue_dist >= min_hue and rgb_dist >= min_rgb:
            return _rgb_to_hex(rgb)

        # Otherwise keep the "best" candidate as fallback.
        # Weighted score: prioritize hue separation, then rgb distance.
        score = (hue_dist * 2.0) + (rgb_dist / 2.0)
        if score > best_score:
            best_score = score
            best_rgb = rgb

        # If we're failing too long, relax slightly (still keeps separation strong).
        if attempt in (60, 120, 180):
            min_hue = max(12.0, min_hue - 2.0)
            min_rgb = max(60.0, min_rgb - 5.0)

    return _rgb_to_hex(best_rgb)


def normalize_camid(label: str) -> str:
    """Normalize camid/video id strings to a consistent form."""
    if not label:
        return ""
    return str(label).strip().lower()


# -----------------------------
# Core Dataclasses
# -----------------------------

@dataclass(frozen=True)
class SkillStep:
    number: int
    name: str

    def to_dict(self) -> Dict:
        return {"number": int(self.number), "name": str(self.name)}

    @staticmethod
    def from_dict(d: Dict) -> "SkillStep":
        return SkillStep(number=int(d["number"]), name=str(d["name"]))


@dataclass
class VideoItem:
    """
    Represents a single video in a session.

    video_id is the logical id used throughout the UI: "video-1", "video-2", ...
    filename is the stored filename in the session folder (typically "video-1.mp4", ...).
    source_type is "local" or "url".
    source is the original local path or URL (informational; may be omitted for privacy).
    """
    video_id: str
    filename: str
    source_type: str  # "local" | "url"
    source: str = ""  # original path or url (optional)
    duration_ms: int = 0  # best-effort; 0 if unknown
    fps: float = 30.0     # best-effort; defaults to 30

    def to_dict(self) -> Dict:
        return {
            "video_id": self.video_id,
            "filename": self.filename,
            "source_type": self.source_type,
            "source": self.source,
            "duration_ms": int(self.duration_ms),
            "fps": float(self.fps),
        }

    @staticmethod
    def from_dict(d: Dict) -> "VideoItem":
        return VideoItem(
            video_id=str(d["video_id"]),
            filename=str(d["filename"]),
            source_type=str(d.get("source_type", "local")),
            source=str(d.get("source", "")),
            duration_ms=int(d.get("duration_ms", 0)),
            fps=float(d.get("fps", 30.0)),
        )


@dataclass
class AnnotationRecord:
    """
    A single annotated step interval.
    Times are stored in seconds, aligned to the chosen time_source video clock.
    Frames are derived from time_source fps.
    """
    label: str
    camid: str            # which video(s) were visible/selected when annotated (e.g., "video-1,video-2")
    step_no: int
    step_name: str

    start_frame: int
    end_frame: int
    total_frames: int

    start_time: float     # seconds
    end_time: float
    total_time: float

    time_source: str      # "video-1" etc.
    audio_source: str     # "video-1" etc.

    confidence: int = 5   # 1-10
    notes: str = ""

    def to_dict(self) -> Dict:
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
    def from_dict(d: Dict) -> "AnnotationRecord":
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
    """
    In-memory state for the currently loaded or created session.
    """
    root_dir: Optional[str] = None
    label: Optional[str] = None
    session_dir: Optional[str] = None

    videos: List[VideoItem] = field(default_factory=list)
    annotations: List[AnnotationRecord] = field(default_factory=list)

    # UI selections
    active_view_ids: List[str] = field(default_factory=list)  # multi-select list of video_ids
    time_source_id: Optional[str] = None
    audio_source_id: Optional[str] = None

    # Pending step capture (annotation workflow)
    pending_step: Optional[SkillStep] = None
    pending_camid: Optional[str] = None          # selected views at start_step time
    pending_start_ms: Optional[int] = None       # from time source player at confirm_start
    pending_time_source: Optional[str] = None
    pending_audio_source: Optional[str] = None
    pending_color: Optional[str] = None          # convenience for highlight

    def clear_pending(self) -> None:
        self.pending_step = None
        self.pending_camid = None
        self.pending_start_ms = None
        self.pending_time_source = None
        self.pending_audio_source = None
        self.pending_color = None

    def is_loaded(self) -> bool:
        return bool(self.root_dir and self.label and self.session_dir)

    def video_ids(self) -> List[str]:
        return [v.video_id for v in self.videos]

    def get_video(self, video_id: str) -> Optional[VideoItem]:
        vid = normalize_camid(video_id)
        for v in self.videos:
            if normalize_camid(v.video_id) == vid:
                return v
        return None

    def ensure_default_sources(self) -> None:
        """Ensure time/audio sources are set to a valid video if possible."""
        ids = self.video_ids()
        if not ids:
            self.time_source_id = None
            self.audio_source_id = None
            self.active_view_ids = []
            return

        if not self.active_view_ids:
            self.active_view_ids = [ids[0]]

        if not self.time_source_id or self.time_source_id not in ids:
            self.time_source_id = ids[0]

        if not self.audio_source_id or self.audio_source_id not in ids:
            self.audio_source_id = ids[0]


# -----------------------------
# Config payload
# -----------------------------

@dataclass
class RootConfig:
    """
    Stored in <root_dir>/config.json
    """
    root_dir: str
    skills: List[SkillStep] = field(default_factory=list)

    # stable mapping: step_no -> palette index (0..49). Keeps colors consistent over time.
    skill_color_map: Dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "data_root": self.root_dir,
            "skills": [s.to_dict() for s in self.skills],
            "skill_color_map": {str(k): int(v) for k, v in self.skill_color_map.items()},
            "palette_version": 1,
        }

    @staticmethod
    def from_dict(d: Dict) -> "RootConfig":
        root = str(d.get("data_root") or d.get("root_dir") or "")
        skills = [SkillStep.from_dict(x) for x in (d.get("skills") or [])]
        raw_map = d.get("skill_color_map") or {}
        color_map: Dict[int, int] = {}
        for k, v in raw_map.items():
            try:
                color_map[int(k)] = int(v)
            except Exception:
                continue
        return RootConfig(root_dir=root, skills=skills, skill_color_map=color_map)


# -----------------------------
# Color assignment helpers
# -----------------------------

def get_skill_color_hex(step_no: int, cfg: Optional[RootConfig]) -> str:
    """
    Returns a stable color for a given step number using cfg.skill_color_map.
    If cfg is None, falls back to deterministic palette selection.
    """
    if step_no is None:
        return SKILL_COLOR_PALETTE_50[0]

    palette_n = len(SKILL_COLOR_PALETTE_50)

    if cfg is not None:
        step_no_i = int(step_no)
        idx = cfg.skill_color_map.get(step_no_i)

        if idx is None:
            # assign next available palette index deterministically
            used = set(int(x) for x in cfg.skill_color_map.values())
            for i in range(palette_n):
                if i not in used:
                    cfg.skill_color_map[step_no_i] = i
                    idx = i
                    break

            if idx is None:
                # Palette exhausted: allocate a new "generated" color index >= palette_n.
                # This avoids wrapping and prevents new labels from reusing palette hues.
                next_idx = max(used) + 1 if used else palette_n
                if next_idx < palette_n:
                    next_idx = palette_n
                cfg.skill_color_map[step_no_i] = next_idx
                idx = next_idx

        idx_i = int(idx)
        if idx_i < palette_n:
            return SKILL_COLOR_PALETTE_50[idx_i % palette_n]

        # Generated colors for indices >= palette size
        gen_index = idx_i - palette_n
        return _generated_color_hex(gen_index)

    # No config provided: keep the old deterministic wrap behavior.
    return SKILL_COLOR_PALETTE_50[int(step_no) % palette_n]


def encode_camid_from_active_views(active_view_ids: List[str]) -> str:
    """
    Encodes active views as a CSV-like string for camid field.
    Example: ["video-1","video-2"] -> "video-1,video-2"
    """
    clean = [normalize_camid(x) for x in (active_view_ids or []) if normalize_camid(x)]
    # keep original-ish casing "video-1" by not lowercasing output too aggressively
    # but normalize spacing/duplicates
    seen = set()
    out: List[str] = []
    for x in clean:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return ",".join(out)


def decode_camid_to_list(camid: str) -> List[str]:
    if not camid:
        return []
    parts = [p.strip() for p in str(camid).split(",")]
    return [normalize_camid(p) for p in parts if normalize_camid(p)]