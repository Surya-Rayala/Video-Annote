"""Synthetic media with ground truth, for pipeline tests.

Every video frame encodes its own index as a 12-bit pattern of vertical bars (white = 1), so any
decoded frame says exactly which source frame it is. The audio carries 20 ms 1 kHz beeps at known
times, so audio/video offsets can be measured to about a millisecond.
"""
from __future__ import annotations

import subprocess
from array import array
from typing import List, Optional, Sequence

from video_annote.media.ffmpeg import find_toolchain

BITS = 12
BEEP_S = 0.02


def _run(cmd: Sequence[str]) -> bytes:
    proc = subprocess.run(list(cmd), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-2000:])
    return proc.stdout


def make_media(
    path: str,
    *,
    duration: float = 4.0,
    fps: str = "25",
    size: str = "320x240",
    vcodec: Sequence[str] = ("-c:v", "libx264", "-crf", "12", "-g", "50"),
    pix_fmt: str = "yuv420p",
    audio: Optional[Sequence[str]] = ("-c:a", "aac", "-b:a", "128k"),
    sample_rate: int = 48000,
    channels: int = 1,
    beep_every: float = 1.0,
    beep_offset: float = 0.5,
    video_delay: float = 0.0,
    audio_delay: float = 0.0,
    vfr_jitter: bool = False,
    late_frames: bool = False,
    burst: int = 0,
    merged_start: int = 0,
    gap: Sequence[int] = (),
    extra: Sequence[str] = (),
) -> str:
    """Write a test clip. ``audio=None`` produces a video-only file. With ``channels=2`` the audio is
    dual-mono (identical channels), like the problem-room recordings."""
    ff = find_toolchain().ffmpeg
    video = (
        f"color=c=black:s={size}:r={fps}:d={duration},format=gray,"
        f"geq=lum='16+219*gt(bitand(N\\,pow(2\\,floor(X*{BITS}/W)))\\,0)'"
    )
    beep = (f"if(lt(mod(t-{beep_offset}\\,{beep_every})\\,{BEEP_S})*gte(t\\,{beep_offset})\\,"
            f"0.5*sin(2*PI*1000*t)\\,0)")
    cmd: List[str] = [ff, "-v", "error", "-y"]
    if video_delay:
        cmd += ["-itsoffset", str(video_delay)]
    cmd += ["-f", "lavfi", "-i", video]
    if audio is not None:
        if audio_delay:
            cmd += ["-itsoffset", str(audio_delay)]
        cmd += ["-f", "lavfi", "-i", f"aevalsrc='{beep}':s={sample_rate}:d={duration}"]
    vf = ["format=" + pix_fmt]
    if vfr_jitter:
        # irregular timestamps: every 7th frame arrives 15 ms late
        vf.append("settb=1/90000,setpts='(N/FRAME_RATE+if(eq(mod(N\\,7)\\,3)\\,0.015\\,0))/TB'")
        cmd += ["-fps_mode", "passthrough"]
    elif late_frames:
        # a constant-rate file in which a few later frames are stamped 0.6 frame late (as the good-room
        # cameras do now and then)
        vf.append("settb=1/90000,setpts='(N+if(eq(N\\,60)+eq(N\\,100)\\,0.6\\,0))/FRAME_RATE/TB'")
        cmd += ["-fps_mode", "passthrough"]
    elif burst:
        # the first frames arrive all at once at t=0, as some cameras start their recordings
        vf.append(f"settb=1/90000,setpts='if(lt(N\\,{burst})\\,N*0.00002\\,N/FRAME_RATE)/TB'")
        cmd += ["-fps_mode", "passthrough"]
    elif merged_start or gap:
        # the first ``merged_start`` frames are stamped within microseconds and the cadence goes on as
        # if they were one frame (as some of the lab's cameras do); ``gap`` = (frame, count): that
        # many frame slots are missing before that frame
        at, count = (tuple(gap) or (0, 0))
        m = max(1, merged_start)
        vf.append(f"settb=1/90000,setpts='if(lt(N\\,{m})\\,N*0.000011\\,"
                  f"(N-{m - 1}+{count}*gte(N\\,{at}))/FRAME_RATE)/TB'")
        cmd += ["-fps_mode", "passthrough"]
    if vfr_jitter or late_frames or burst or merged_start or gap:
        # a fine time base, or the encoder rounds the shifts away
        cmd += ["-enc_time_base", "1:90000", "-video_track_timescale", "90000"]
    cmd += ["-map", "0:v:0", "-vf", ",".join(vf), *vcodec]
    if audio is not None:
        cmd += ["-map", "1:a:0", "-ac", str(channels), *audio]
    cmd += [*extra, path]
    _run(cmd)
    return path


def apple_priming(path: str, start_ms: int, priming: int) -> None:
    """Rewrite the audio edit list of an MP4 that ffmpeg wrote (an empty edit, then the media from its
    first sample) as Apple's recorders write AAC: the audio starts after an empty edit of
    ``start_ms`` and its first ``priming`` samples (encoder warm-up) are skipped."""
    import struct

    with open(path, "rb") as f:
        data = bytearray(f.read())

    def boxes(start: int, end: int):
        while start + 8 <= end:
            size, kind = struct.unpack(">I4s", data[start:start + 8])
            yield kind.decode("latin1"), start + 8, start + size
            start += size

    def find(start: int, end: int, path_: List[str]):
        for kind, body, stop in boxes(start, end):
            if kind == path_[0]:
                yield (body, stop) if len(path_) == 1 else from_(find(body, stop, path_[1:]))

    def from_(gen):
        return next(gen, None)

    moov = from_(find(0, len(data), ["moov"]))
    mvhd = from_(find(*moov, ["mvhd"]))
    version = data[mvhd[0]]
    movie_scale = struct.unpack(">I", data[mvhd[0] + (20 if version == 1 else 12):][:4])[0]  # (ffmpeg 9 picks its own)
    for kind, body, stop in boxes(*moov):
        if kind != "trak":
            continue
        hdlr = from_(find(body, stop, ["mdia", "hdlr"]))
        if hdlr is None or bytes(data[hdlr[0] + 8:hdlr[0] + 12]) != b"soun":
            continue
        elst = from_(find(body, stop, ["edts", "elst"]))
        version, count = data[elst[0]], struct.unpack(">I", data[elst[0] + 4:elst[0] + 8])[0]
        assert version == 0 and count == 2, (version, count)  # an empty edit and the media
        rate = 48000  # (the factory's default sample rate)
        first, second = elst[0] + 8, elst[0] + 20
        media = struct.unpack(">I", data[second:second + 4])[0]
        data[first:first + 8] = struct.pack(">Ii", start_ms * movie_scale // 1000, -1)
        data[second:second + 8] = struct.pack(">Ii", media - priming * movie_scale // rate, priming)
        with open(path, "wb") as f:
            f.write(bytes(data))
        return
    raise ValueError("no audio track")


def frame_indices(path: str) -> List[int]:
    """Decode every frame and read back the index pattern."""
    ff = find_toolchain().ffmpeg
    raw = _run([ff, "-v", "error", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
                "-vf", f"scale={BITS}:1:flags=area,format=gray", "-f", "rawvideo", "-"])
    return [
        sum(1 << b for b in range(BITS) if raw[i * BITS + b] > 125)
        for i in range(len(raw) // BITS)
    ]


def frame_times(path: str) -> List[float]:
    """Presentation timestamps (s) of all video frames, as ffmpeg's decoder sees them."""
    ffprobe = find_toolchain().ffprobe
    out = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                "frame=best_effort_timestamp_time", "-of", "default=noprint_wrappers=1:nokey=1",
                path]).decode()
    return [float(x) for x in out.split() if x.strip() and x.strip() != "N/A"]


def audio_samples(path: str, sample_rate: int = 48000) -> array:
    """First audio stream decoded to mono float32 on the presentation timeline: sample i is at
    i / sample_rate seconds (a stream that starts late is padded with silence)."""
    ff = find_toolchain().ffmpeg
    raw = _run([ff, "-v", "error", "-i", path, "-map", "0:a:0", "-af", "aresample=async=1:first_pts=0",
                "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-acodec", "pcm_f32le", "-"])
    samples = array("f")
    samples.frombytes(raw)
    return samples


def beep_onsets(samples: array, sample_rate: int = 48000, threshold: float = 0.1,
                min_gap_s: float = 0.2) -> List[float]:
    """Times (s) where the signal first exceeds ``threshold`` after at least ``min_gap_s`` of quiet."""
    onsets: List[float] = []
    last_loud = -1e9
    gap = int(min_gap_s * sample_rate)
    for i, v in enumerate(samples):
        if abs(v) >= threshold:
            if i - last_loud > gap:
                onsets.append(i / sample_rate)
            last_loud = i
    return onsets


def difference_peak_db(a: str, b: str, clip_a: bool = False, timeline: bool = True) -> float:
    """Peak level (dBFS) of (audio of a) - (audio of b), both decoded on the presentation timeline
    (or, with ``timeline=False``, simply sample after sample). -inf means sample-identical; a
    misalignment of even one sample shows up as a large value. ``clip_a`` limits a to full scale
    first (a lossless integer copy cannot hold decoded overs)."""
    ff = find_toolchain().ffmpeg
    clip = r",aeval=clip(val(0)\,-1\,1)" if clip_a else ""
    place = ",aresample=async=1:first_pts=0" if timeline else ""
    # channel 0 of each (dual-mono sources are stored as exact mono)
    graph = (f"[0:a:0]pan=mono|c0=c0{place},aformat=sample_fmts=dbl{clip}[x];"
             f"[1:a:0]pan=mono|c0=c0{place},aformat=sample_fmts=dbl,volume=-1[y];"
             "[x][y]amix=inputs=2:normalize=0:duration=shortest,astats=metadata=0[out]")
    proc = subprocess.run([ff, "-hide_banner", "-nostdin", "-i", a, "-i", b, "-filter_complex", graph,
                           "-map", "[out]", "-f", "null", "-"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    overall = proc.stderr.rsplit("Overall", 1)[-1]
    for line in overall.splitlines():
        if "Peak level dB:" in line:
            value = line.split("Peak level dB:", 1)[1].strip()
            return float("-inf") if value == "-inf" else float(value)
    raise RuntimeError("no astats output")


def audio_timestamp_gaps(path: str, tolerance_s: float = 0.002) -> int:
    """Number of places where the audio packets' timestamps jump ahead (holes in the recording's
    audio, as some eye-tracker recordings have)."""
    ff = find_toolchain()
    out = subprocess.run([ff.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "packet=pts_time,duration_time", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout
    gaps, end = 0, None
    for line in out.splitlines():
        try:
            pts, dur = (float(x) for x in line.split(",")[:2])
        except ValueError:
            continue
        if end is not None and pts - end > tolerance_s:
            gaps += 1
        end = pts + dur
    return gaps


def audio_duration_on_timeline(path: str) -> float:
    """Length of the audio decoded on the presentation timeline (seconds)."""
    ff = find_toolchain().ffmpeg
    raw = _run([ff, "-v", "error", "-i", path, "-map", "0:a:0", "-af",
                "pan=mono|c0=c0,aresample=async=1:first_pts=0", "-f", "s16le", "-ar", "8000", "-"])
    return len(raw) / 2 / 8000

