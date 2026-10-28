# video_annote/media/probe.py
"""Typed view of what ffprobe reports about a media file."""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .ffmpeg import ffprobe_json

# Nominal rates we snap to when a measured rate is within SNAP_TOLERANCE of them (e.g. 29.9999 -> 30,
# 24.95 -> 25). Keeps frame <-> time math exact for the usual camera rates.
STANDARD_RATES: List[Fraction] = [
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
    Fraction(48000, 1001), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
    Fraction(10), Fraction(12), Fraction(25, 2), Fraction(15000, 1001), Fraction(15), Fraction(20),
    Fraction(100), Fraction(120000, 1001), Fraction(120), Fraction(240000, 1001), Fraction(240),
]
# 1%: a short variable-rate file whose last frame has no duration (ffmpeg 7.1 writes such files)
# averages 1/N fast, e.g. 30.25 fps for 120 frames at 30 fps
SNAP_TOLERANCE = 0.01
MIN_FPS, MAX_FPS = 1, 240


def parse_rate(text: Any) -> Optional[Fraction]:
    """Parse ffprobe rates such as "30000/1001", "25/1", "25" -> Fraction; "0/0" or junk -> None."""
    try:
        s = str(text).strip()
        if "/" in s:
            num, den = s.split("/", 1)
            if int(den) == 0:
                return None
            value = Fraction(int(num), int(den))
        else:
            value = Fraction(s).limit_denominator(1001)
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return value if MIN_FPS <= value <= MAX_FPS else None


def _float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def snap_rate(rate: Fraction) -> Fraction:
    """Nearest standard rate within SNAP_TOLERANCE (30.001 -> 30, not 29.97), else the rate itself."""
    error, best = min((abs(float(rate / std) - 1.0), std) for std in STANDARD_RATES)
    return best if error <= SNAP_TOLERANCE else rate.limit_denominator(1001)


@dataclass(frozen=True)
class VideoStream:
    index: int
    codec: str
    profile: str
    width: int
    height: int
    pix_fmt: str
    color_range: str
    field_order: str
    r_frame_rate: Optional[Fraction]
    avg_frame_rate: Optional[Fraction]
    start_time: float
    duration: Optional[float]
    frame_count: Optional[int]
    rotation: int
    bits_per_sample: Optional[int]
    duration_from_tag: bool = False  # see _tag_duration: may be the stream's end time, not its length

    @property
    def is_full_range(self) -> bool:
        return self.color_range == "pc" or self.pix_fmt.startswith("yuvj")

    @property
    def is_interlaced(self) -> bool:
        return self.field_order in ("tt", "bb", "tb", "bt")

    @property
    def is_variable_rate(self) -> bool:
        r, a = self.r_frame_rate, self.avg_frame_rate
        return bool(r and a and abs(float(r / a) - 1.0) > 0.001)

    def exact_frame_rate(self) -> Optional[Fraction]:
        """The file's own frame rate, unsnapped (for working copies we already produced)."""
        r, a = self.r_frame_rate, self.avg_frame_rate
        if r and a and r == a:
            return r
        return a or r

    def nominal_frame_rate(self) -> Optional[Fraction]:
        """Frame rate to use for a constant-frame-rate working copy.

        For CFR input r_frame_rate == avg_frame_rate. For VFR input r_frame_rate is the timestamp
        granularity: when it is a plausible ceiling (not wildly above the average) we keep it so no
        frames are dropped; otherwise the average is used. The result is snapped to a standard rate."""
        r, a = self.r_frame_rate, self.avg_frame_rate
        if r and a and r == a and (r.denominator in (1, 1001) or r.denominator <= 2):
            return r  # an exactly declared constant rate: keep it (e.g. 15000/1001)
        if r and a:
            base = r if (r > a * Fraction(101, 100) and r <= a * 2) else a
        else:
            base = a or r
        if base is None and self.frame_count and self.duration:
            base = parse_rate(Fraction(self.frame_count) / Fraction(self.duration).limit_denominator(100000))
        return snap_rate(base) if base else None


@dataclass(frozen=True)
class AudioStream:
    index: int
    codec: str
    profile: str
    sample_rate: int
    channels: int
    channel_layout: str
    start_time: float
    duration: Optional[float]
    bit_rate: Optional[int]


@dataclass(frozen=True)
class MediaInfo:
    path: str
    format_name: str
    duration: Optional[float]
    start_time: float
    video: Optional[VideoStream]
    audio: Optional[AudioStream]
    extra_video_streams: int = 0
    extra_audio_streams: int = 0
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def video_duration(self) -> Optional[float]:
        if self.video and self.video.duration:
            return self.video.duration
        return self.duration

    def summary(self) -> str:
        parts = []
        if self.video:
            v = self.video
            fps = v.nominal_frame_rate()
            parts.append(
                f"{v.codec} {v.width}x{v.height} {v.pix_fmt}"
                + (f" {float(fps):.3f} fps" if fps else "")
                + (" VFR" if v.is_variable_rate else "")
            )
        if self.audio:
            a = self.audio
            parts.append(f"{a.codec} {a.sample_rate} Hz {a.channel_layout or str(a.channels) + 'ch'}")
        else:
            parts.append("no audio")
        if self.duration:
            parts.append(f"{self.duration:.2f} s")
        return ", ".join(parts)


def _tag_duration(stream: Dict[str, Any]) -> Optional[float]:
    """Matroska/WebM keep stream durations in a DURATION tag ("00:01:02.345000000"). Writers differ:
    ffmpeg's muxer (also used by OBS) stores the stream's end time, mkvmerge its length; the two only
    differ for streams that do not start at 0."""
    for key, value in (stream.get("tags") or {}).items():
        if key.upper().startswith("DURATION"):
            try:
                h, m, sec = str(value).split(":")
                return int(h) * 3600 + int(m) * 60 + float(sec)
            except ValueError:
                return None
    return None


def _rotation(stream: Dict[str, Any]) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            return int(round(_float(sd.get("rotation")) or 0)) % 360
    return int(_float((stream.get("tags") or {}).get("rotate")) or 0) % 360


def _video_stream(s: Dict[str, Any]) -> VideoStream:
    return VideoStream(
        index=int(s.get("index", 0)),
        codec=str(s.get("codec_name") or "unknown"),
        profile=str(s.get("profile") or ""),
        width=_int(s.get("width")) or 0,
        height=_int(s.get("height")) or 0,
        pix_fmt=str(s.get("pix_fmt") or ""),
        color_range=str(s.get("color_range") or "unknown"),
        field_order=str(s.get("field_order") or "unknown"),
        r_frame_rate=parse_rate(s.get("r_frame_rate")),
        avg_frame_rate=parse_rate(s.get("avg_frame_rate")),
        start_time=_float(s.get("start_time")) or 0.0,
        duration=_float(s.get("duration")) or _tag_duration(s),
        frame_count=_int(s.get("nb_frames")),
        rotation=_rotation(s),
        bits_per_sample=_int(s.get("bits_per_raw_sample")),
        duration_from_tag=_float(s.get("duration")) is None and _tag_duration(s) is not None,
    )


def _audio_stream(s: Dict[str, Any]) -> AudioStream:
    return AudioStream(
        index=int(s.get("index", 0)),
        codec=str(s.get("codec_name") or "unknown"),
        profile=str(s.get("profile") or ""),
        sample_rate=_int(s.get("sample_rate")) or 0,
        channels=_int(s.get("channels")) or 0,
        channel_layout=str(s.get("channel_layout") or ""),
        start_time=_float(s.get("start_time")) or 0.0,
        duration=_float(s.get("duration")),
        bit_rate=_int(s.get("bit_rate")),
    )


def _pick(streams: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Prefer the stream flagged as default, else the first one."""
    if not streams:
        return None
    return next((s for s in streams if (s.get("disposition") or {}).get("default") == 1), streams[0])


def _pick_video(streams: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Highest-resolution video stream (an HLS master playlist lists every variant, often the
    smallest first); ties go to the default/first stream."""
    if not streams:
        return None
    best = max((_int(s.get("width")) or 0) * (_int(s.get("height")) or 0) for s in streams)
    return _pick([s for s in streams if (_int(s.get("width")) or 0) * (_int(s.get("height")) or 0) == best])


def parse_probe(path: str, data: Dict[str, Any]) -> MediaInfo:
    streams = data.get("streams") or []
    videos = [
        s for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    audios = [s for s in streams if s.get("codec_type") == "audio" and (_int(s.get("channels")) or 0) > 0]
    fmt = data.get("format") or {}
    v, a = _pick_video(videos), _pick(audios)
    return MediaInfo(
        path=path,
        format_name=str(fmt.get("format_name") or ""),
        duration=_float(fmt.get("duration")),
        start_time=_float(fmt.get("start_time")) or 0.0,
        video=_video_stream(v) if v else None,
        audio=_audio_stream(a) if a else None,
        extra_video_streams=max(0, len(videos) - 1),
        extra_audio_streams=max(0, len(audios) - 1),
        raw=data,
    )


def probe(path: str, timeout: float = 120.0) -> MediaInfo:
    data = ffprobe_json(path, "-show_format", "-show_streams", timeout=timeout)
    return parse_probe(path, data)
