# video_annote/core/timecode.py
"""Time/frame conversions, annotation field recomputation and timeline lane stacking."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import List, Union

from .models import DEFAULT_FPS, AnnotationRecord

Rate = Union[float, Fraction]


def _fps(fps: Rate) -> float:
    try:
        f = float(fps)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return f if f > 0 else DEFAULT_FPS


def ms_to_time_str(ms: int) -> str:
    """Milliseconds -> "MM:SS" (minutes keep counting past 59)."""
    s = max(0, int(ms or 0)) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def ms_to_precise_str(ms: int) -> str:
    """Milliseconds -> "MM:SS.mmm"."""
    ms = max(0, int(ms or 0))
    return f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}.{ms % 1000:03d}"


def parse_time(text: str) -> float:
    """Seconds from a typed time: "64.5", "01:04.500" (as the UI shows times) or "1:01:04.5".
    Raises ValueError for anything else."""
    parts = [p.strip() for p in str(text).strip().split(":")]
    if not 1 <= len(parts) <= 3 or not all(parts):
        raise ValueError(f"not a time: {text!r}")
    seconds = float(parts[-1])
    whole = [int(p) for p in parts[:-1]]  # [hours,] minutes
    if whole and (not 0 <= seconds < 60 or min(whole) < 0 or (len(whole) == 2 and whole[1] >= 60)):
        raise ValueError(f"not a time: {text!r}")
    value = sum(n * 60 ** (len(whole) - i) for i, n in enumerate(whole)) + seconds
    if not math.isfinite(value):
        raise ValueError(f"not a time: {text!r}")
    return value


def seconds_to_frames(seconds: float, fps: Rate) -> int:
    """Frame number stored in label.tsv: round(t * fps). Kept for compatibility with 0.1.x data."""
    return int(round(float(seconds or 0.0) * _fps(fps)))


def frames_to_seconds(frames: int, fps: Rate) -> float:
    return float(frames or 0) / _fps(fps)


def displayed_frame(seconds: float, fps: Rate) -> int:
    """Index of the frame on screen at time ``seconds`` in a constant-frame-rate video starting at 0:
    frame k is shown during [k / fps, (k + 1) / fps). Stored times have millisecond precision, so a
    frame-start time such as 1/30 s is saved as 0.033; half a millisecond of slack keeps it on its frame."""
    return max(0, int(math.floor((float(seconds or 0.0) + 0.0005) * _fps(fps) + 1e-9)))


def snap_to_frame_start(seconds: float, fps: Rate) -> float:
    """Start time of the frame on screen at ``seconds`` (so time and frame number always agree)."""
    return displayed_frame(seconds, fps) / _fps(fps)


def _exact(fps: Rate) -> Fraction:
    f = fps if isinstance(fps, Fraction) else Fraction(_fps(fps)).limit_denominator(1001)
    return f if f > 0 else Fraction(int(DEFAULT_FPS))


def frame_at_ms(position_ms: int, fps: Rate) -> int:
    """Exact index of the frame on screen at a *player position* in whole milliseconds (no slack:
    at 30 fps frame 61 starts at 2033.33 ms, so 2033 ms still shows frame 60)."""
    f = _exact(fps)
    return max(0, (int(position_ms) * f.numerator) // (1000 * f.denominator))


def frame_start_ms(frame: int, fps: Rate) -> int:
    """First whole millisecond at which ``frame`` is on screen."""
    f = _exact(fps)
    return -((-int(frame) * 1000 * f.denominator) // f.numerator)  # ceil(frame * 1000 / fps)


def frame_centre_ms(frame: int, fps: Rate) -> int:
    """Whole millisecond in the middle of ``frame``: the position to seek to in order to show it.
    Never seek to a frame's start: Qt's AVFoundation backend converts positions through float32
    and can land microseconds early, which shows the previous frame."""
    f = _exact(fps)
    return ((2 * int(frame) + 1) * 500 * f.denominator) // f.numerator  # floor((frame + 0.5) * 1000 / fps)


def recompute_from_times(rec: AnnotationRecord, fps: Rate) -> AnnotationRecord:
    """Recompute frames and totals from start_time/end_time (clamped at 0). Times are kept with the
    3 decimals label.tsv stores, so a record in memory maps to the same frames as after reloading."""
    st = round(max(0.0, float(rec.start_time)), 3)
    et = round(max(0.0, float(rec.end_time)), 3)
    if et < st:
        raise ValueError("end_time must be >= start_time")
    sf, ef = seconds_to_frames(st, fps), seconds_to_frames(et, fps)
    return replace(rec, start_time=st, end_time=et, total_time=et - st,
                   start_frame=sf, end_frame=ef, total_frames=max(ef - sf, 0))


def recompute_from_frames(rec: AnnotationRecord, fps: Rate) -> AnnotationRecord:
    """Recompute times and totals from start_frame/end_frame (clamped at 0)."""
    sf = max(0, int(rec.start_frame))
    ef = max(0, int(rec.end_frame))
    if ef < sf:
        raise ValueError("end_frame must be >= start_frame")
    st, et = round(frames_to_seconds(sf, fps), 3), round(frames_to_seconds(ef, fps), 3)
    return replace(rec, start_frame=sf, end_frame=ef, total_frames=ef - sf,
                   start_time=st, end_time=et, total_time=et - st)


# -----------------------------
# Lane stacking for overlap rendering
# -----------------------------

@dataclass(frozen=True)
class Block:
    """A timeline block in milliseconds; ``idx`` points into the annotation list."""
    idx: int
    start_ms: int
    end_ms: int


def stack_blocks_into_lanes(blocks: List[Block]) -> List[List[Block]]:
    """Greedy lane assignment: earliest start first, each block goes into the first lane whose last
    block ends at or before its start (touching edges do not overlap)."""
    norm = sorted(
        (Block(b.idx, min(b.start_ms, b.end_ms), max(b.start_ms, b.end_ms)) for b in blocks),
        key=lambda b: (b.start_ms, b.end_ms - b.start_ms),
    )
    lanes: List[List[Block]] = []
    for b in norm:
        for lane in lanes:
            if lane[-1].end_ms <= b.start_ms:
                lane.append(b)
                break
        else:
            lanes.append([b])
    return lanes


def annotations_to_blocks_ms(annotations: List[AnnotationRecord]) -> List[Block]:
    return [
        Block(i, int(round(float(r.start_time) * 1000.0)), int(round(float(r.end_time) * 1000.0)))
        for i, r in enumerate(annotations)
    ]


def compute_lanes_for_annotations(annotations: List[AnnotationRecord]) -> List[List[Block]]:
    return stack_blocks_into_lanes(annotations_to_blocks_ms(annotations))
