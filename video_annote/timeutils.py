# video_annote/timeutils.py
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List

from .domain import AnnotationRecord


# -----------------------------
# Time formatting / conversion
# -----------------------------

def ms_to_time_str(ms: int) -> str:
    if ms is None:
        ms = 0
    ms = max(0, int(ms))
    s = ms // 1000
    m = s // 60
    s = s % 60
    return f"{m:02d}:{s:02d}"


def seconds_to_time_str(sec: float) -> str:
    if sec is None:
        sec = 0.0
    return ms_to_time_str(int(round(float(sec) * 1000.0)))


def seconds_to_frames(seconds: float, fps: float) -> int:
    if fps is None or fps <= 0:
        fps = 30.0
    if seconds is None:
        seconds = 0.0
    return int(round(float(seconds) * float(fps)))


def frames_to_seconds(frames: int, fps: float) -> float:
    if fps is None or fps <= 0:
        fps = 30.0
    if frames is None:
        frames = 0
    return float(frames) / float(fps)


# -----------------------------
# Annotation recompute logic
# -----------------------------

def recompute_from_times(rec: AnnotationRecord, fps: float) -> AnnotationRecord:
    """Given start_time/end_time, recompute frames + totals. Validates ordering."""
    if fps is None or fps <= 0:
        fps = 30.0

    st = float(rec.start_time)
    et = float(rec.end_time)
    if st < 0:
        st = 0.0
    if et < 0:
        et = 0.0
    if et < st:
        raise ValueError("end_time must be >= start_time")

    sf = seconds_to_frames(st, fps)
    ef = seconds_to_frames(et, fps)

    return replace(
        rec,
        start_time=st,
        end_time=et,
        total_time=(et - st),
        start_frame=sf,
        end_frame=ef,
        total_frames=max(ef - sf, 0),
    )


def recompute_from_frames(rec: AnnotationRecord, fps: float) -> AnnotationRecord:
    """Given start_frame/end_frame, recompute times + totals. Validates ordering."""
    if fps is None or fps <= 0:
        fps = 30.0

    sf = int(rec.start_frame)
    ef = int(rec.end_frame)
    if sf < 0:
        sf = 0
    if ef < 0:
        ef = 0
    if ef < sf:
        raise ValueError("end_frame must be >= start_frame")

    st = frames_to_seconds(sf, fps)
    et = frames_to_seconds(ef, fps)

    return replace(
        rec,
        start_frame=sf,
        end_frame=ef,
        total_frames=max(ef - sf, 0),
        start_time=st,
        end_time=et,
        total_time=(et - st),
    )


def recompute_totals(rec: AnnotationRecord) -> AnnotationRecord:
    """Safe total recompute when both time and frame are already consistent."""
    st = max(0.0, float(rec.start_time))
    et = max(0.0, float(rec.end_time))
    if et < st:
        et = st
    sf = max(0, int(rec.start_frame))
    ef = max(0, int(rec.end_frame))
    if ef < sf:
        ef = sf

    return replace(
        rec,
        start_time=st,
        end_time=et,
        total_time=(et - st),
        start_frame=sf,
        end_frame=ef,
        total_frames=max(ef - sf, 0),
    )


# -----------------------------
# Lane stacking for overlap rendering
# -----------------------------

@dataclass(frozen=True)
class Block:
    """A simplified block representation for timeline rendering (ms)."""
    idx: int
    start_ms: int
    end_ms: int


def stack_blocks_into_lanes(blocks: List[Block]) -> List[List[Block]]:
    """
    Greedy lane assignment:
      - Sort by start time then duration
      - Place each block into first lane that doesn't overlap
      - If none fits, create new lane

    Touching edges are allowed (end == next start is not an overlap).
    """
    if not blocks:
        return []

    norm: List[Block] = []
    for b in blocks:
        s = int(b.start_ms)
        e = int(b.end_ms)
        if e < s:
            s, e = e, s
        norm.append(Block(idx=int(b.idx), start_ms=s, end_ms=e))

    norm.sort(key=lambda x: (x.start_ms, (x.end_ms - x.start_ms)))

    lanes: List[List[Block]] = []
    for b in norm:
        placed = False
        for lane in lanes:
            last = lane[-1]
            if last.end_ms <= b.start_ms:
                lane.append(b)
                placed = True
                break
        if not placed:
            lanes.append([b])

    return lanes


def annotations_to_blocks_ms(annotations: List[AnnotationRecord]) -> List[Block]:
    out: List[Block] = []
    for i, rec in enumerate(annotations):
        s = int(round(float(rec.start_time) * 1000.0))
        e = int(round(float(rec.end_time) * 1000.0))
        out.append(Block(idx=i, start_ms=s, end_ms=e))
    return out


def compute_lanes_for_annotations(annotations: List[AnnotationRecord]) -> List[List[Block]]:
    return stack_blocks_into_lanes(annotations_to_blocks_ms(annotations))