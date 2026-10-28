# video_annote/media/analysis.py
"""Measure audio characteristics that matter for annotation and ML export.

A single decode pass through ffmpeg filters (no numpy needed at runtime) yields level, peak,
integrated loudness, leading digital silence, whether a stereo track is really dual-mono, and whether
a steady mains hum/buzz is present (narrow bands on the 50/60/100/120 Hz harmonic grids compared with
the bands halfway between harmonics).
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from .ffmpeg import FFmpegError, find_toolchain, run_capture

# Integrated loudness below this is reported as "very quiet" (broadcast reference is -23 LUFS;
# typical close-mic speech sits around -16..-30 LUFS).
QUIET_LUFS = -40.0
SILENCE_DB = -90.0  # treat as digital silence
# (fundamental, harmonics checked). 100/120 Hz is the full-wave "buzz" of 50/60 Hz mains.
# 4 families x 3 harmonics x (on, between) = 24 bands, the size of ffmpeg's largest named layout.
HUM_FAMILIES = ((50, 3), (60, 3), (100, 3), (120, 3))
HUM_LAYOUT = "22.2"
HUM_MEAN_DB = 5.0  # average harmonic prominence over the in-between bands
HUM_LOW_DB = 8.0  # ... and of the first two harmonics


def _hum_bands():
    for f0, count in HUM_FAMILIES:
        for k in range(1, count + 1):
            yield f0, k, k * f0, (k + 0.5) * f0


@dataclass(frozen=True)
class AudioStats:
    rms_dbfs: Optional[float]
    peak_dbfs: Optional[float]
    loudness_lufs: Optional[float]
    loudness_range_lu: Optional[float]
    true_peak_dbfs: Optional[float]
    leading_silence_s: float
    dual_mono: Optional[bool]  # None when the track is not 2-channel
    hum_hz: Optional[int] = None  # fundamental of a detected steady hum/buzz (50/60/100/120)
    hum_prominence_db: Optional[float] = None

    def to_dict(self) -> Dict:
        return asdict(self)

    def warnings(self, stored_as_alac: bool = False) -> List[str]:
        """Unusual conditions, worded for the user; ``stored_as_alac`` when the import decodes the
        audio and stores it as ALAC (it may then limit decoded peaks above full scale)."""
        out: List[str] = []
        if self.peak_dbfs is None:
            return ["the audio track is digital silence (no sound was recorded)"]
        if self.loudness_lufs is not None and self.loudness_lufs < QUIET_LUFS:
            out.append(
                f"audio is very quiet ({self.loudness_lufs:.1f} LUFS integrated, peak "
                f"{self.peak_dbfs:.1f} dBFS); it is left as recorded (not normalized)"
            )
        if self.peak_dbfs >= -0.1:
            where = ("in the lossless working copy" if stored_as_alac else
                     "in exported MP4 clips and 16/24-bit WAVs (32-bit float WAVs keep them)")
            out.append(f"audio reaches full scale (peak {self.peak_dbfs:+.1f} dBFS): the recording may be clipped"
                       + (f"; decoded peaks above full scale (a lossy codec can produce them) are limited to "
                          f"full scale {where}" if self.peak_dbfs > 0 else ""))
        if self.leading_silence_s >= 0.1:
            out.append(f"audio starts with {self.leading_silence_s:.2f} s of digital silence (present in the source)")
        if self.dual_mono:
            out.append("stereo track carries identical left/right channels (dual-mono)")
        if self.hum_hz:
            out.append(
                f"steady {self.hum_hz} Hz hum/buzz locked to the mains frequency in the recording "
                f"(harmonics {self.hum_prominence_db:.0f} dB above neighbouring bands; electrical pickup in "
                "the capture chain or nearby mains-powered equipment); it is left as recorded (not filtered)"
            )
        return out


    def notes(self) -> List[str]:
        """What someone using the recording should know, in plain words (see warnings for details)."""
        if self.peak_dbfs is None:
            return ["The audio track is silent."]
        out: List[str] = []
        if self.loudness_lufs is not None and self.loudness_lufs < QUIET_LUFS:
            out.append("The audio is very quiet.")
        if self.peak_dbfs >= -0.1:
            out.append("The audio reaches full scale in places, so it may be clipped.")
        if self.hum_hz:
            out.append(f"The audio has a steady {self.hum_hz} Hz electrical buzz (left as recorded).")
        return out


def _num(text: str) -> Optional[float]:
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _overall(lines: List[str], instance: str) -> Dict[str, str]:
    """Key/values printed by an astats instance after its 'Overall' marker."""
    out: Dict[str, str] = {}
    seen_overall = False
    for line in lines:
        if f"[astats@{instance} @" not in line:
            continue
        body = line.split("]", 1)[-1].strip()
        if body == "Overall":
            seen_overall = True
            continue
        if seen_overall and ":" in body:
            key, value = body.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def _detect_hum(log: str):
    """(fundamental, prominence dB) of the strongest hum family, or (None, None)."""
    levels = {}
    channel = None
    for line in log.splitlines():
        if "[astats@hum @" not in line:
            continue
        body = line.split("]", 1)[-1].strip()
        if body.startswith("Channel:"):
            channel = int(body.split(":", 1)[1]) - 1
        elif body.startswith("RMS level dB:") and channel is not None:
            value = body.split(":", 1)[1].strip()
            levels[channel] = -200.0 if value == "-inf" else float(value)
    best = (None, None)
    bands = list(_hum_bands())
    for f0, _count in HUM_FAMILIES:
        diffs = [levels.get(2 * i, -200.0) - levels.get(2 * i + 1, -200.0)
                 for i, b in enumerate(bands) if b[0] == f0]
        if not diffs:
            continue
        mean = sum(diffs) / len(diffs)
        low = sum(diffs[:2]) / 2
        if mean >= HUM_MEAN_DB and low >= HUM_LOW_DB and (best[1] is None or mean > best[1]):
            best = (f0, round(mean, 1))
    return best


def parse_analysis_log(log: str, stereo: bool) -> AudioStats:
    lines = log.splitlines()
    level = _overall(lines, "level")
    diff = _overall(lines, "diff") if stereo else {}

    leading = 0.0
    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*(-?[\d.]+)", log)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([\d.]+)", log)]
    if starts and starts[0] <= 0.001 and ends:
        leading = ends[0]

    summary = log.rsplit("Summary:", 1)[-1] if "Summary:" in log else ""

    def grab(pattern: str) -> Optional[float]:
        m = re.search(pattern, summary)
        return _num(m.group(1)) if m else None

    dual_mono: Optional[bool] = None
    if stereo:  # identical means bit-identical: the left-minus-right signal is exactly zero
        dual_mono = diff.get("Peak level dB", "") == "-inf"

    hum_hz, hum_db = _detect_hum(log)
    loudness = grab(r"I:\s*(-?[\d.]+|-inf)\s*LUFS")
    if loudness is not None and loudness <= -70.0:  # ebur128's floor: silent or too short to gate
        loudness = None
    return AudioStats(
        rms_dbfs=_num(level.get("RMS level dB", "")),
        peak_dbfs=_num(level.get("Peak level dB", "")),
        loudness_lufs=loudness,
        loudness_range_lu=grab(r"LRA:\s*(-?[\d.]+)\s*LU"),
        true_peak_dbfs=grab(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS"),
        leading_silence_s=round(leading, 3),
        dual_mono=dual_mono,
        hum_hz=hum_hz,
        hum_prominence_db=hum_db,
    )


def analyze_audio(path: str, channels: int, timeout: float = 600.0, stream_index: Optional[int] = None) -> AudioStats:
    """Decode one audio stream once and measure it (by default the first; pass the absolute stream
    index of the track that will be kept when a file has several)."""
    stereo = channels == 2
    branches = 5 if stereo else 4
    source = f"[0:{stream_index}]" if stream_index is not None else "[0:a:0]"
    graph = [f"{source}asplit={branches}[a][c][d][h]" + ("[b]" if stereo else "")]
    graph.append("[a]astats@level=metadata=0[oa]")
    graph.append(f"[c]silencedetect@sil=n={SILENCE_DB}dB:d=0.05[oc]")
    graph.append("[d]ebur128@loud=peak=true:framelog=verbose[od]")
    outs = ["[oa]", "[oc]", "[od]"]
    if stereo:
        graph.append("[b]pan=mono|c0=c0-c1,astats@diff=metadata=0[ob]")
        outs.append("[ob]")
    # hum: two narrow band-passes (in series) per harmonic and per in-between frequency. Mains
    # harmonics of interest are below 1 kHz, so this runs at 4 kHz; all bands are merged into one
    # multichannel stream measured by a single astats (one output instead of dozens is much faster).
    freqs = [f for _f0, _k, on, off in _hum_bands() for f in (on, off)]
    # (in floating point: with 16-bit input the narrow filters would run in integer arithmetic, and their
    # rounding noise on a quiet recording looks like hum)
    graph.append("[h]pan=mono|c0=c0,aresample=4000,aformat=sample_fmts=dblp,asplit=" + str(len(freqs))
                 + "".join(f"[h{i}]" for i in range(len(freqs))))
    for i, f in enumerate(freqs):
        graph.append(f"[h{i}]bandpass=f={f}:width_type=h:w=2,bandpass=f={f}:width_type=h:w=2[g{i}]")
    graph.append("".join(f"[g{i}]" for i in range(len(freqs))) + f"join=inputs={len(freqs)}:"
                 f"channel_layout={HUM_LAYOUT},"
                 "astats@hum=metadata=0:measure_overall=none:measure_perchannel=RMS_level,"
                 "pan=mono|c0=FL[oh]")
    outs.append("[oh]")
    cmd = [find_toolchain().ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-i", path,
           "-filter_complex", ";".join(graph)]
    for o in outs:
        cmd += ["-map", o, "-f", "null", "-"]
    proc = run_capture(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise FFmpegError("audio analysis failed", "\n".join(proc.stderr.splitlines()[-15:]))
    return parse_analysis_log(proc.stderr, stereo)
