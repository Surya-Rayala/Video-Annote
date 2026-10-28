# video_annote/media/ingest.py
"""Turn a source recording into the session's working copy.

The working copy is what the annotator watches and what segments are exported from, so it must be
decodable by Qt's media backends, seek quickly, and keep the source's timing and audio intact:

* Video is re-encoded to 8-bit 4:2:0 H.264 at the source's nominal frame rate as constant frame rate
  (frame k is shown during [k/fps, (k+1)/fps)), with a short keyframe interval so scrubbing is
  responsive. Resolution is kept; interlaced input is deinterlaced; rotation is applied.
* Audio is never put through a second lossy encoder. AAC and ALAC tracks are copied bit-for-bit.
  Anything else (MP3, PCM, Opus, ...) is decoded once and stored losslessly as ALAC (24-bit, or
  16-bit for 16-bit PCM), keeping the sample rate. So is AAC whose encoder priming the source skips
  in a way a copy cannot keep (Apple's recorders: macOS screen recordings, iPhone videos), and the
  working copy states where its audio starts in samples, so it plays exactly as in the source. MP3 cannot simply be copied: Apple's media stack,
  which Qt uses on macOS, does not expose an MP3 track inside MP4 at all, so playback would be
  silent. A stereo track whose two channels are identical (dual-mono) is stored as exact mono.
* The output is written to a temporary file, validated against the source (duration, frame count,
  audio presence and timing) and only then moved into place.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .analysis import analyze_audio
from .ffmpeg import CancelCheck, FFmpegError, ProgressCallback, ffmpeg_version, ffprobe_json, run_ffmpeg
from .probe import SNAP_TOLERANCE, STANDARD_RATES, MediaInfo, probe, snap_rate
from .recorded import Stamp, copy_start, find_recording_time, iso_utc, recorded_block

log = logging.getLogger("video_annote")

COPYABLE_AUDIO = {"aac", "alac"}  # MP4 audio every Qt backend can play
PCM_16BIT_OR_LESS = {"pcm_s16le", "pcm_s16be", "pcm_u8", "pcm_s8", "pcm_alaw", "pcm_mulaw"}
AAC_SAMPLE_RATES = {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000}
NETWORK_PREFIXES = ("http://", "https://")
VIDEO_CODEC_TAGS = ("avc", "hvc", "hev", "vp8", "vp9", "vp09", "av01", "mp4v", "dvh", "dva", "theora")  # HLS CODECS
# The only layouts the ALAC encoder accepts, by channel count. Other layouts (2.1, quad, 7.1, unknown
# 3-channel ...) would be converted to the nearest of these, dropping or mixing channels.
ALAC_LAYOUTS = {1: "mono", 2: "stereo", 3: "3.0", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1(back)", 8: "7.1(wide)"}


def unknown_layout(layout: str) -> bool:
    """ffprobe names a layout it does not know "" or "N channels"."""
    return not layout or layout == "unknown" or layout.endswith("channels")


def alac_layout_filter(channels: int, layout: str = "") -> str:
    """Filter that relabels ``channels`` channels, one to one and without mixing, as the ALAC layout
    with the same channel count ("" when no relabelling is needed). Raises ValueError above 8."""
    target = ALAC_LAYOUTS.get(channels)
    if target is None:
        raise ValueError(f"{channels} audio channels cannot be stored as ALAC (at most 8)")
    if channels <= 2 or layout == target:
        return ""
    return f"channelmap=map={'|'.join(str(i) for i in range(channels))}:channel_layout={target}"


def audio_span(info: MediaInfo) -> Dict[str, float]:
    """{"start_s", "end_s"}: where a working copy's audio starts and ends on its timeline ({} if
    unknown). WAV exports keep to that span instead of padding a label with silence."""
    a = info.audio
    if a is None or not a.duration:
        return {}
    return {"start_s": round(a.start_time, 6), "end_s": round(a.start_time + a.duration, 6)}


def keeps_timestamps(info: MediaInfo) -> bool:
    """Whether a source is converted with its own timestamps (-copyts): files whose audio starts
    before 0 because of codec pre-roll (AAC priming, Opus pre-skip; typical of Matroska/WebM).
    Otherwise ffmpeg 8.0 and older shift every stream by the pre-roll, and placing the video on the
    constant-rate grid then leaves it 7-19 ms off the audio. The MP4 keeps the pre-roll as an edit list."""
    return info.start_time < 0 and info.video is not None and info.video.start_time >= 0


class IngestError(RuntimeError):
    """The source cannot be imported, or the produced working copy failed validation."""


@dataclass(frozen=True)
class IngestOptions:
    crf: int = 18
    preset: str = "veryfast"
    keyframe_interval_s: float = 1.0
    audio_mode: str = "auto"  # "auto": copy AAC/ALAC, store others losslessly (ALAC) | "aac": re-encode to AAC
    aac_bitrate_per_channel: int = 96_000
    analyze: bool = True
    network_timeout_s: float = 30.0


@dataclass(frozen=True)
class IngestPlan:
    frame_rate: Fraction
    video_filters: Tuple[str, ...]
    video_args: Tuple[str, ...]
    audio_action: str  # "copy" | "lossless" | "transcode" | "none"
    audio_args: Tuple[str, ...]
    notes: Tuple[str, ...] = ()
    timed: bool = False  # the rate was taken from the frame timestamps (see plan_ingest)
    rate_assumed: bool = False  # no frame rate could be read: 30 fps is assumed

    def describe(self) -> str:
        audio = {"copy": "audio copied unchanged", "lossless": "audio decoded once and stored losslessly (ALAC)",
                 "transcode": "audio re-encoded to AAC", "none": "no audio"}
        return f"video re-encoded (H.264, {float(self.frame_rate):g} fps CFR); {audio[self.audio_action]}"


@dataclass
class IngestResult:
    duration_ms: int
    frame_rate: Fraction
    frame_count: int
    media: Dict[str, Any] = field(default_factory=dict)
    ingest: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)  # everything worth noting, technical (see notes)
    recorded: Dict[str, Any] = field(default_factory=dict)  # real-world start/end (see media.recorded)
    notes: List[str] = field(default_factory=list)  # what people need to know, in plain words


class HlsPlaylist(NamedTuple):
    """What _hls_playlist reads from the media playlist of the imported variant."""
    total_s: float  # the stated length
    precision_s: float  # how exactly the segment lengths state it
    unit_s: float  # the coarsest rounding of a segment length
    program_date_time: Optional[str] = None  # wall-clock time of the first segment, as written


def plan_ingest(info: MediaInfo, options: IngestOptions = IngestOptions(), dual_mono: bool = False,
                cadence: Optional[Fraction] = None, lost_priming_s: float = 0.0) -> IngestPlan:
    """Decide how each stream is handled, from what the probe (and, if known, the audio analysis:
    ``dual_mono``, the standard rate the first frames' timestamps follow: ``cadence``, see _cadence,
    and the encoder priming a copy of the audio would play: ``lost_priming_s``, see _lost_priming)
    reported. Pure function."""
    v = info.video
    if v is None:
        raise IngestError("The file has no video stream.")
    rate = v.nominal_frame_rate()
    notes: List[str] = []
    stated = [x for x in (v.r_frame_rate, v.avg_frame_rate) if x]
    near = [x for x in stated if cadence and abs(float(x / cadence) - 1) <= SNAP_TOLERANCE]
    # the stated rates disagree and the timestamps follow one of them, or ffprobe states no average and a
    # timestamp grid twice their rate: a camera's start-up burst read through HLS makes ffprobe state
    # twice its rate, and planning at that would repeat every frame
    timed = bool(cadence) and (rate is None or (abs(float(rate / cadence) - 1) > SNAP_TOLERANCE and bool(near))
                               or (v.avg_frame_rate is None and v.r_frame_rate == 2 * cadence))
    if timed:
        rate = snap_rate(near[0]) if near else cadence
        notes.append(f"frame rate {float(rate):g} fps taken from the frame timestamps"
                     + (f" (the file states {' and '.join(f'{float(x):g}' for x in stated)} fps)" if stated else ""))
    rate_assumed = rate is None
    if rate is None:
        rate = Fraction(30)
        notes.append("frame rate could not be determined; assumed 30 fps")
    elif v.is_variable_rate and not timed:
        notes.append(f"variable frame rate source converted to constant {float(rate):g} fps")

    filters: List[str] = []
    if v.is_interlaced:
        filters.append("yadif=mode=send_frame")
        notes.append("interlaced source deinterlaced")
    # every frame goes to the constant-rate slot nearest to its timestamp. (-fps_mode cfr alone only
    # corrects once a frame is more than 1.1 frames off, so a camera's start-up burst or a frame
    # stamped late could leave the whole rest of a recording one or two frames behind its audio.)
    filters.append(f"fps=fps={rate.numerator}/{rate.denominator}")
    if v.width % 2 or v.height % 2:
        filters.append("crop=trunc(iw/2)*2:trunc(ih/2)*2")
    if v.is_full_range:
        # full-range (JPEG) YUV -> limited range, the H.264 default every player expects
        filters.append("scale=out_range=tv")
    filters.append("format=yuv420p")

    gop = max(1, round(float(rate) * options.keyframe_interval_s))
    video_args = (
        "-c:v", "libx264", "-preset", options.preset, "-crf", str(options.crf),
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-fps_mode", "cfr", "-r", f"{rate.numerator}/{rate.denominator}",
        "-color_range", "tv",
    )

    a = info.audio
    if a is None:
        return IngestPlan(rate, tuple(filters), video_args, "none", ("-an",), tuple(notes), timed, rate_assumed)
    if options.audio_mode == "auto":
        if a.codec in COPYABLE_AUDIO and not lost_priming_s:
            return IngestPlan(rate, tuple(filters), video_args, "copy", ("-c:a", "copy"), tuple(notes), timed, rate_assumed)
        if a.codec in COPYABLE_AUDIO:
            notes.append(f"{a.codec.upper()} audio decoded once and stored losslessly: a copy would play the encoder's "
                         f"{lost_priming_s * 1000:.0f} ms warm-up that the source skips")
        # 16-bit (or narrower) PCM fits ALAC's 16-bit mode exactly; decoded lossy audio is float, so
        # keep 24 bits of it (s32p input makes the ALAC encoder write 24-bit samples).
        sample_fmt = "s16p" if a.codec in PCM_16BIT_OR_LESS else "s32p"
        mono = dual_mono and a.channels == 2
        if mono:
            notes.append("identical left/right channels stored once (exact mono)")
        try:
            relabel = "" if mono else alac_layout_filter(a.channels, a.channel_layout)
        except ValueError:
            raise IngestError(f"The audio track has {a.channels} channels; the working copy can keep at most 8 "
                              "losslessly. Extract the channels you need first.") from None
        if relabel:
            notes.append(f"{a.channels}-channel audio ({a.channel_layout or 'unknown layout'}) stored channel by "
                         f"channel as {ALAC_LAYOUTS[a.channels]} (ALAC's layout for {a.channels} channels)")
        af = "pan=mono|c0=c0" if mono else relabel
        audio_args = (*(("-af", af) if af else ()), "-c:a", "alac", "-sample_fmt", sample_fmt)
        return IngestPlan(rate, tuple(filters), video_args, "lossless", audio_args, tuple(notes), timed, rate_assumed)

    channels = 1 if dual_mono and a.channels == 2 else max(1, a.channels)
    bitrate = min(max(options.aac_bitrate_per_channel * channels, 128_000), 512_000)
    audio_args: List[str] = ["-af", "pan=mono|c0=c0"] if channels == 1 and a.channels == 2 else []
    if channels > 8 or (channels >= 6 and unknown_layout(a.channel_layout)):
        # AAC holds at most 8 channels, and for 6-8 unlabelled ones it would code one as LFE (bass only)
        raise IngestError(f"The audio has {channels} {'unlabelled ' if channels <= 8 else ''}channels, which AAC "
                          "cannot keep as they are; import without --audio aac (lossless) instead.")
    if channels >= 3 and unknown_layout(a.channel_layout):  # an LFE-free layout of the same count
        audio_args += ["-af", alac_layout_filter(channels)]
    audio_args += ["-c:a", "aac", "-b:a", str(bitrate)]
    if a.sample_rate not in AAC_SAMPLE_RATES:
        audio_args += ["-ar", "48000"]
        notes.append(f"audio resampled from {a.sample_rate} Hz to 48000 Hz (not an AAC rate)")
    return IngestPlan(rate, tuple(filters), video_args, "transcode", tuple(audio_args), tuple(notes), timed, rate_assumed)


def build_command(source: str, dest: str, info: MediaInfo, plan: IngestPlan, options: IngestOptions,
                  creation_time: str = "") -> List[str]:
    """Arguments for run_ffmpeg (everything after the global options). ``creation_time``: the
    recording's start, kept in the copy's own metadata (ffmpeg does not carry it over; MP4 stores
    whole seconds, session.json the full precision)."""
    args: List[str] = []
    if source.lower().startswith(NETWORK_PREFIXES):
        args += ["-rw_timeout", str(int(options.network_timeout_s * 1_000_000))]
    if keeps_timestamps(info):
        args += ["-copyts"]
    args += ["-i", source, "-map", f"0:{info.video.index}"]  # type: ignore[union-attr]
    if info.audio is not None:
        args += ["-map", f"0:{info.audio.index}"]
    args += ["-vf", ",".join(plan.video_filters), *plan.video_args, *plan.audio_args]
    # Do NOT pass -avoid_negative_ts: shifting timestamps to make B-frame DTS non-negative delays the
    # video by the reorder depth (80 ms with x264 defaults at 25 fps) but the audio by a different
    # amount, and breaks the priming edit list of copied audio. The defaults keep frame 0 at t=0.
    if creation_time:
        args += ["-metadata", f"creation_time={creation_time}"]
    if info.audio is not None and info.audio.sample_rate > 0:
        # edit lists in samples: where the audio starts stays exact (in milliseconds, the default, an audio
        # track that starts 87.979 ms in, as macOS screen recordings do, would start 0.979 ms early)
        rate = 48000 if "-ar" in plan.audio_args else info.audio.sample_rate
        args += ["-movie_timescale", str(rate)]
    args += ["-movflags", "+faststart", "-f", "mp4", dest]
    return args


def _first_frame_times(source: str, info: MediaInfo, timeout: float) -> List[float]:
    """Timestamps (s) of the first 40 video frames in presentation order, from the first 60 packets
    (they are in decoding order, so only the first 40 frames are certain to be among them); [] if they
    cannot be read."""
    if info.video is None:
        return []
    try:
        data = ffprobe_json(source, "-select_streams", str(info.video.index), "-read_intervals", "%+#60",
                            "-show_entries", "packet=pts_time", timeout=timeout)
    except FFmpegError:
        return []
    times = []
    for packet in data.get("packets") or []:
        try:
            times.append(float(packet["pts_time"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(times)[:40]


def _lost_priming(source: str, info: MediaInfo, timeout: float) -> float:
    """Seconds of encoder priming that the source's player skips but a copy of its audio would play:
    an edit list that skips priming after an empty edit (as Apple's recorders write AAC) reaches the
    muxer only as "skip samples" on the first packet, which the MP4 muxer drops. A copy keeps only
    the part of it stamped before t = 0. 0 if nothing would be lost or it cannot be read."""
    a = info.audio
    if a is None or not a.sample_rate:
        return 0.0
    try:
        data = ffprobe_json(source, "-select_streams", str(a.index), "-read_intervals", "%+#1",
                            "-show_entries", "packet=pts_time:packet_side_data", timeout=timeout)
        first = (data.get("packets") or [{}])[0]
        pts = float(first.get("pts_time"))
        skip = max((int(d.get("skip_samples") or 0) for d in first.get("side_data_list") or []), default=0)
    except (FFmpegError, TypeError, ValueError):
        return 0.0
    lost = skip / a.sample_rate - max(0.0, -pts)
    return round(lost, 6) if lost > 0.5 / a.sample_rate else 0.0


def _cadence(times: List[float]) -> Optional[Fraction]:
    """The standard frame rate the first frames' timestamps follow regularly (a start-up burst and
    pauses aside), or None."""
    steps = sorted(b - a for a, b in zip(times, times[1:]) if b - a > 0.001)
    if len(steps) < 10:
        return None
    median = steps[len(steps) // 2]
    regular = [d for d in steps if abs(d / median - 1) <= 0.1]
    if len(regular) < 0.8 * len(steps):
        return None
    rate = snap_rate(Fraction(len(regular) / sum(regular)).limit_denominator(100000))
    return rate if rate in STANDARD_RATES else None


def _merged_at_start(times: List[float], info: MediaInfo, rate: Fraction) -> int:
    """Frames at the start of a source that share a slot of the constant-rate grid with another
    frame: a camera's start-up burst (several frames stamped at almost the same moment, as some
    good-room cameras do). The conversion keeps one frame per slot, so they are dropped by design;
    this keeps them from hiding real gaps or looking like lost frames. ``times``: see
    _first_frame_times."""
    if info.video is None or len(times) < 2:
        return 0
    origin = 0.0 if keeps_timestamps(info) else info.start_time
    slots = [round((t - origin) * float(rate)) for t in times]
    return max(0, len(times) - (slots[-1] - slots[0] + 1))


def _constant_rate(info: MediaInfo, plan: IngestPlan, burst: int) -> bool:
    """Whether the source's frames sit on the plan's constant-rate grid, so that frames repeated to
    fill it mean missing frames: sources that state one rate, and those whose rate was taken from
    their timestamps because a start-up burst (``burst``, see _merged_at_start) or a misjudged
    average made the stated rates disagree. Truly variable-rate sources repeat and drop by design."""
    v = info.video
    if v is None:
        return False
    if not v.is_variable_rate:
        return True
    r = v.r_frame_rate
    return plan.timed and (burst > 0 or bool(r and abs(float(r / plan.frame_rate) - 1) <= SNAP_TOLERANCE))


def _missing_frames(src: MediaInfo, plan: IngestPlan, run: Any, merged: int = 0,
                    constant: Optional[bool] = None) -> int:
    """Frames missing from a constant-rate source's timeline (gaps), as the conversion counted them:
    frames repeated beyond those it dropped (timing jitter repeats one frame and drops another; a
    start-up burst, ``merged``, only drops) and beyond the lead-in of a late-starting video; reported
    above 2 frames. They are filled by repeating the previous frame. ``constant``: see
    _constant_rate (default: the source states one rate)."""
    if src.video is None or not (constant if constant is not None else not src.video.is_variable_rate):
        return 0
    origin = 0.0 if keeps_timestamps(src) else src.start_time
    lead = max(0, round((src.video.start_time - origin) * float(plan.frame_rate)))
    missing = (run.filter_dups - max(0, run.filter_drops - merged)) + max(0, run.dup_frames - lead)
    return missing if missing > 2 else 0


def _hls_playlist(source: str, info: MediaInfo, timeout: float) -> Optional[HlsPlaylist]:
    """Total length, its precision and the coarsest rounding (seconds) of the HLS media playlist the
    import reads, and the wall-clock time of its first segment if stated; None if it cannot be read.
    Each segment length (EXTINF) is rounded to its last written digit (RFC 8216: to the nearest), e.g.
    to whole seconds in playlists before protocol version 3 or written with ffmpeg's round_durations.
    A master playlist is followed to the variant of the imported video: the first with its
    resolution, else the one with the highest bandwidth."""
    import http.client
    from urllib.parse import urljoin
    from urllib.request import urlopen

    remote = source.lower().startswith(NETWORK_PREFIXES)

    def lines_of(where: str) -> Tuple[str, List[str]]:  # (location after redirects, lines)
        if remote:
            with urlopen(where, timeout=timeout) as response:
                where, text = response.geturl(), response.read(8_000_000).decode("utf-8", "replace")
        else:
            with open(where, encoding="utf-8", errors="replace") as f:
                text = f.read(8_000_000)
        return where, [line.strip() for line in text.splitlines()]

    try:
        source, lines = lines_of(source)
        variants = []  # (resolution matches, has video, bandwidth, -position, uri)
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                uri = next((x for x in lines[i + 1:] if x and not x.startswith("#")), "")
                size = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                bandwidth = re.search(r"[:,]BANDWIDTH=(\d+)", line)
                codecs = re.search(r'CODECS="([^"]*)"', line)
                match = bool(size and info.video and (int(size.group(1)), int(size.group(2)))
                             == (info.video.width, info.video.height))
                video = not codecs or any(c.strip().lower().startswith(VIDEO_CODEC_TAGS)
                                          for c in codecs.group(1).split(","))
                variants.append((match, video, int(bandwidth.group(1)) if bandwidth else 0, -i, uri))
        if variants:  # a master playlist: the probe picks the first stream of the largest resolution
            matching = [v for v in variants if v[0]]
            uri = (max(matching, key=lambda v: v[3]) if matching else max(variants, key=lambda v: v[1:4]))[4]
            _, lines = lines_of(urljoin(source, uri) if remote else os.path.join(os.path.dirname(source), uri))
        values = [line[len("#EXTINF:"):].split(",", 1)[0].strip() for line in lines if line.startswith("#EXTINF:")]
        units = [10.0 ** -len(v.split(".", 1)[1]) if "." in v else 1.0 for v in values]
        if not values:
            return None
        date = None  # an EXT-X-PROGRAM-DATE-TIME before the first segment dates that segment's first sample
        for line in lines:
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                date = line.split(":", 1)[1].strip()
            elif line and not line.startswith("#"):
                break
        return HlsPlaylist(sum(float(v) for v in values), sum(u / 2 for u in units), max(units), date)
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _validate(src: MediaInfo, out: MediaInfo, plan: IngestPlan, merged: int = 0,
              playlist: Optional[HlsPlaylist] = None, gap: int = 0,
              constant: Optional[bool] = None) -> List[str]:
    """Compare the working copy with its source; raise on real defects, return soft warnings.
    ``merged``: start-up frames the conversion drops by design (see _merged_at_start);
    ``playlist``: an HLS stream's playlist (see _hls_playlist);
    ``gap``: the reported frames that fill gaps in the source's timeline (see _missing_frames);
    ``constant``: see _constant_rate (default: the source states one rate)."""
    if out.video is None:
        raise IngestError("The converted file has no video stream.")
    if src.audio is not None and out.audio is None:
        raise IngestError("The converted file lost its audio stream.")
    assert src.video is not None
    warnings: List[str] = []
    frame = 1.0 / float(plan.frame_rate)
    out_dur = out.video_duration or 0.0
    origin = 0.0 if keeps_timestamps(src) else src.start_time  # source time of the copy's t=0
    offset = max(0.0, src.video.start_time - origin)  # the copy holds frame 0 from t=0
    if src.audio is not None and out.audio is not None:
        expected_channels = 1 if plan.audio_action != "copy" and "pan=mono" in " ".join(plan.audio_args) \
            else src.audio.channels
        if out.audio.channels != expected_channels:
            raise IngestError(f"The converted audio has {out.audio.channels} channels instead of {expected_channels}.")
    hls = "hls" in src.format_name
    slack = max(2 * frame, 0.1)
    if hls:
        # the length the playlist of the imported variant states, as exact as its segment lengths; only a
        # copy shorter than that means lost data (packagers that state the target length, e.g. 6.000 for
        # 6.006 s segments at 29.97 fps, make complete copies longer)
        if playlist is None:
            warnings.append("the stream's playlist could not be read to check its precision, so completeness was "
                            "not checked")
        else:
            expected, precision, unit = playlist[:3]
            slack += precision
            if precision >= 0.5:
                warnings.append(f"the stream's playlist states its segment lengths rounded to {unit:g} s, so its "
                                f"completeness could only be checked to within {slack:.1f} s")
            if expected - out_dur > slack:
                raise IngestError(f"Converted video lasts {out_dur:.3f} s but the stream's playlist states "
                                  f"{expected:.3f} s: the stream is probably incomplete or damaged.")
    elif src.video.duration:
        duration = src.video.duration
        expected = offset + duration
        if src.video.duration_from_tag:  # the tag may be the end time: accept either reading
            expected = min((expected, duration - origin), key=lambda e: abs(out_dur - e))
        if abs(out_dur - expected) > slack:
            raise IngestError(f"Converted video lasts {out_dur:.3f} s but the source video ends at "
                              f"{expected:.3f} s: the source is probably incomplete or damaged.")
    else:
        warnings.append("the source does not state its video duration, so completeness was not checked")
    if out.video.start_time > frame / 2:
        warnings.append(f"working copy video starts at {out.video.start_time:.3f} s")
    constant = constant if constant is not None else not src.video.is_variable_rate
    if (constant and src.video.frame_count and offset < frame / 2
            and out.video.frame_count and abs(out.video.frame_count - (src.video.frame_count - merged + gap)) > 1):
        # AVI headers state the frame count, while the readable duration shrinks with a truncated file
        if "avi" in src.format_name.split(",") \
                and out.video.frame_count < src.video.frame_count - max(2, src.video.frame_count // 100):
            raise IngestError(f"The source declares {src.video.frame_count} frames but only {out.video.frame_count} "
                              "could be read: it is probably truncated or damaged.")
        warnings.append(f"working copy has {out.video.frame_count} frames, the source {src.video.frame_count}")
    if src.format_name.startswith("mpegts") or hls:  # stream durations are estimates, or the playlist's (rounded)
        return warnings
    if src.audio is not None and out.audio is not None:
        if src.audio.duration and out.audio.duration and abs(out.audio.duration - src.audio.duration) > 0.1:
            warnings.append(
                f"audio duration changed from {src.audio.duration:.3f} s to {out.audio.duration:.3f} s"
            )
    if src.audio and src.video and src.audio.duration and src.video.duration:
        gap = src.audio.duration + src.audio.start_time - (src.video.duration + src.video.start_time)
        if abs(gap) >= 0.1:
            warnings.append(
                f"in the source, audio {'outlasts' if gap > 0 else 'ends before'} video by {abs(gap):.2f} s"
            )
    return warnings


def ingest(
    source: str,
    dest_path: str,
    options: IngestOptions = IngestOptions(),
    on_progress: Optional[ProgressCallback] = None,
    should_cancel: Optional[CancelCheck] = None,
    start: Optional[Stamp] = None,
) -> IngestResult:
    """Create the working copy of ``source`` at ``dest_path`` (an .mp4 path). ``start``: when the
    recording started, if known otherwise (e.g. entered by hand); else the time the file states."""
    t0 = time.monotonic()
    remote = source.lower().startswith(NETWORK_PREFIXES)
    timeout = options.network_timeout_s * 4 if remote else 120.0
    info = probe(source, timeout=timeout)
    stats = None
    if options.analyze and info.audio is not None and not remote:
        # measure the source itself (one fast decode); remote sources are measured after download
        try:
            stats = analyze_audio(source, info.audio.channels, stream_index=info.audio.index)
        except FFmpegError:
            stats = None
    times = _first_frame_times(source, info, timeout)
    plan = plan_ingest(info, options, dual_mono=bool(stats and stats.dual_mono), cadence=_cadence(times),
                       lost_priming_s=_lost_priming(source, info, timeout))
    burst = _merged_at_start(times, info, plan.frame_rate)
    constant = _constant_rate(info, plan, burst)
    merged = burst if constant else 0
    playlist = _hls_playlist(source, info, timeout) if "hls" in info.format_name else None
    stamp = start or find_recording_time(source, info, playlist.program_date_time if playlist else None)
    origin = 0.0 if keeps_timestamps(info) else info.start_time  # source time of the copy's t=0

    directory = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(directory, exist_ok=True)
    partial = os.path.join(directory, f".{os.path.basename(dest_path)}.partial.mp4")
    try:
        run = run_ffmpeg(
            build_command(source, partial, info, plan, options,
                          iso_utc(copy_start(stamp, origin, info.start_time)) if stamp is not None else ""),
            duration_s=info.video_duration or info.duration,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
        if run.data_lost:  # e.g. an HLS segment that could not be downloaded: the copy would have a gap
            raise IngestError("Part of the source could not be read, so the copy would contain a gap: "
                              f"{run.data_lost[0]}. Try again, or download the file first.")
        out = probe(partial)
        gap = _missing_frames(info, plan, run, merged, constant)
        warnings = list(plan.notes) + _validate(info, out, plan, merged, playlist, gap, constant)
        notes: List[str] = []  # (the few things worth telling people, in plain words)
        if plan.rate_assumed:
            notes.append("The frame rate could not be read, so 30 frames per second were assumed.")
        if ("hls" in info.format_name and playlist is None) or ("hls" not in info.format_name and not info.video.duration):
            notes.append("It could not be checked that the whole recording was imported.")
        if stamp is not None and stamp.assumed_zone:
            warnings.append(f"{stamp.source} states no time zone, so the recording time was read as this computer's "
                            "local time")
            notes.append("The recording time in the file has no time zone, so it was read as this computer's local "
                         "time.")
        frames = max(1, out.video.frame_count or 0) if out.video else 1
        if gap and remote and "hls" in info.format_name and gap > max(3, frames // 2000):
            # a segment download that stalled and was skipped: no log line says so. (A progressive
            # download that stalls ends early, which the duration check catches; a camera's own few
            # dropped frames stay a warning.)
            raise IngestError(f"About {gap / float(plan.frame_rate):.1f} s of the stream's video is missing (the "
                              "download probably stalled). Try again, or download the file first.")
        if gap:
            warnings.append(f"the source's video has gaps (about {gap} frames, {gap / float(plan.frame_rate):.2f} s, "
                            "are missing); they are filled by repeating the previous frame")
            notes.append(f"About {gap / float(plan.frame_rate):.1f} s of video is missing from the recording; the "
                         "picture holds still there.")
        if run.issues:
            warnings.append(f"the source has decoding errors (damaged parts are concealed or skipped): "
                            f"{run.issues[0]}")
            notes.append("Parts of the recording are damaged; they may look or sound wrong.")
        if stats is None and options.analyze and out.audio is not None:
            try:
                stats = analyze_audio(partial, out.audio.channels)
            except FFmpegError as e:
                warnings.append(f"audio analysis failed: {str(e).splitlines()[0]}")
        if stats is not None:
            warnings += stats.warnings(stored_as_alac=plan.audio_action == "lossless")
            notes += stats.notes()
        os.replace(partial, dest_path)
    finally:
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass

    assert out.video is not None
    rate = out.video.exact_frame_rate() or plan.frame_rate
    if rate != plan.frame_rate:
        warnings.append(f"working copy runs at {float(rate):.4f} fps instead of {float(plan.frame_rate):.4f}")
    frame_count = out.video.frame_count or int(round((out.video_duration or 0.0) * float(rate)))
    if info.extra_audio_streams:
        warnings.append(f"source has {info.extra_audio_streams + 1} audio tracks; only the default one was kept")
        notes.append(f"The recording has {info.extra_audio_streams + 1} audio tracks; only the main one was kept.")
    # only what playing and exporting the copy needs (the rest is in the log, and `video-annote probe`)
    media: Dict[str, Any] = {"video_start_s": round(out.video.start_time, 6), "audio": None}
    if out.audio is not None:
        media["audio"] = {
            "channels": out.audio.channels,
            "channel_layout": out.audio.channel_layout,
            "source_channel_layout": info.audio.channel_layout if info.audio else "",
            "sample_rate": out.audio.sample_rate,
            **audio_span(out),
            "dual_mono": bool(stats and stats.dual_mono),
        }
    recorded = recorded_block(stamp, origin, info.start_time, out.video.start_time + (out.video_duration or 0.0))
    log.info("imported %s as %s: %s%s", source, dest_path, plan.describe(),
             "".join(f"\n  - {w}" for w in warnings))
    return IngestResult(
        duration_ms=int(round((out.video_duration or 0.0) * 1000.0)),
        frame_rate=rate,
        frame_count=frame_count,
        media=media,
        ingest={
            "plan": plan.describe(),
            "video": "transcode",
            "audio": plan.audio_action,
            "ffmpeg": ffmpeg_version(),
            "seconds": round(time.monotonic() - t0, 2),
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "warnings": warnings,
        },
        warnings=warnings,
        recorded=recorded,
        notes=notes,
    )
