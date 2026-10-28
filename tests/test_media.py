"""Media pipeline tests against synthetic clips with ground truth (need ffmpeg/ffprobe)."""
from __future__ import annotations

import bisect
import dataclasses
import hashlib
import re
import subprocess
import wave
from array import array
from fractions import Fraction

import pytest

from media_factory import (audio_samples, beep_onsets, difference_peak_db, frame_indices, frame_times, make_media)
from video_annote.core.models import AnnotationRecord, SessionState, VideoItem
from video_annote.media.analysis import analyze_audio
from video_annote.media.export import ExportOptions, clip_command, export_segments, plan_clips
from video_annote.media.ffmpeg import CancelledError, find_toolchain
from video_annote.media.ingest import IngestOptions, ingest, keeps_timestamps, plan_ingest
from video_annote.media.probe import parse_probe, parse_rate, probe, snap_rate

pytestmark = pytest.mark.media

MP3 = ("-c:a", "libmp3lame", "-b:a", "128k")
AAC = ("-c:a", "aac", "-b:a", "96k")

# (name, factory kwargs, expected audio action)
SOURCES = [
    ("mp3_dualmono_48k_25fps.mp4", dict(audio=MP3, channels=2), "lossless"),  # like the room2-4 cameras
    ("aac_mono_32k_30fps_fullrange.mp4", dict(audio=AAC, sample_rate=32000, fps="30", pix_fmt="yuvj420p"), "copy"),
    ("pcm_stereo_44k.mov", dict(audio=("-c:a", "pcm_s16le"), sample_rate=44100, channels=2), "lossless"),
    ("opus_48k.mkv", dict(audio=("-c:a", "libopus", "-b:a", "96k")), "lossless"),
    ("vfr.mp4", dict(audio=AAC, vfr_jitter=True), "copy"),
    ("video_starts_late.mp4", dict(audio=MP3, sample_rate=24000, video_delay=0.0424), "lossless"),
    ("video_starts_late.mkv", dict(audio=AAC, video_delay=0.5), "copy"),  # DURATION tag = end time
    ("audio_starts_late.mp4", dict(audio=AAC, audio_delay=0.3), "copy"),
    ("late_frames_30fps.mp4", dict(audio=AAC, fps="30", late_frames=True), "copy"),  # like the good rooms
    ("burst_start.mp4", dict(audio=AAC, burst=6), "copy"),
    ("no_audio.mp4", dict(audio=None), "none"),
]


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("src")
    return {name: make_media(str(d / name), **kw) for name, kw, _ in SOURCES}


def _pcm(path, fmt="s16le"):
    """First channel of the first audio stream, decoded at its native rate. (Dual-mono sources are
    stored as exact mono, so channel 0 is what must be preserved.)"""
    ff = find_toolchain().ffmpeg
    return subprocess.run([ff, "-v", "error", "-i", path, "-map", "0:a:0", "-af", "pan=mono|c0=c0",
                           "-f", fmt, "-"], capture_output=True, check=True).stdout


def _pcm_md5(path):
    return hashlib.md5(_pcm(path)).hexdigest()


def _same_pcm(a_path, b_path, max_lag=2048):
    """Decoded audio identical sample for sample. The decoded start may differ by up to one AAC
    priming block: a Matroska source decodes from its pre-roll (before t=0), while the MP4 copy's edit
    list hides it (timing is checked separately, by the beeps)."""
    a, b = array("h"), array("h")
    a.frombytes(_pcm(a_path))
    b.frombytes(_pcm(b_path))
    for lag in range(-max_lag, max_lag + 1):
        x, y = a[max(0, lag):], b[max(0, -lag):]
        n = min(len(x), len(y)) - max_lag
        if n > 0 and x[:n] == y[:n]:
            return True
    return False


def _max_float_diff(a_path, b_path):
    a, b = array("f"), array("f")
    a.frombytes(_pcm(a_path, "f32le"))
    b.frombytes(_pcm(b_path, "f32le"))
    assert len(a) == len(b)
    return max(abs(x - y) for x, y in zip(a, b))


def _frame_at(path, t):
    """Frame on screen at ``t`` as measured by an ffmpeg decode (relative to the container start,
    e.g. -0.021 s for AAC in Matroska); frame times are raw timestamps. Before a late-starting video
    begins, the working copy holds its first frame."""
    times, idx = frame_times(path), frame_indices(path)
    return idx[max(0, bisect.bisect_right(times, t + probe(path).start_time + 1e-6) - 1)]


# ---------------- probe / plan (pure) ----------------

def test_rate_parsing_and_snapping():
    assert parse_rate("30000/1001") == Fraction(30000, 1001)
    assert parse_rate("0/0") is None and parse_rate("90000/1") is None and parse_rate("N/A") is None
    assert snap_rate(Fraction(473895000, 15795971)) == 30  # 30.001 -> 30, not 29.97
    assert snap_rate(Fraction(26810000, 1074343)) == 25  # Tobii average 24.95 -> 25
    assert snap_rate(Fraction(3600, 119)) == 30  # 120 frames at 30 fps, the last one without a duration
    assert snap_rate(Fraction(30000, 1001)) == Fraction(30000, 1001)
    assert snap_rate(Fraction(47)) == 47


def _fake_probe(**video):
    stream = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
              "pix_fmt": "yuv420p", "r_frame_rate": "25/1", "avg_frame_rate": "25/1", **video}
    return parse_probe("x.mp4", {"streams": [
        {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        stream,
        {"index": 2, "codec_type": "audio", "codec_name": "mp3", "sample_rate": "48000", "channels": 2},
    ], "format": {"duration": "10.0"}})


def test_plan_never_adds_a_lossy_audio_generation():
    plan = plan_ingest(_fake_probe())  # MP3 in the source
    assert plan.audio_action == "lossless" and plan.audio_args == ("-c:a", "alac", "-sample_fmt", "s32p")
    dual = plan_ingest(_fake_probe(), dual_mono=True)  # identical channels: stored once, exactly
    assert dual.audio_args == ("-af", "pan=mono|c0=c0", "-c:a", "alac", "-sample_fmt", "s32p")
    aac = _fake_probe()
    aac.raw["streams"][2]["codec_name"] = "aac"
    assert plan_ingest(parse_probe("x.mp4", aac.raw)).audio_action == "copy"
    pcm = _fake_probe()
    pcm.raw["streams"][2]["codec_name"] = "pcm_s16le"
    assert plan_ingest(parse_probe("x.mov", pcm.raw)).audio_args[-1] == "s16p"


def test_plan_builds_cfr_short_gop_video():
    plan = plan_ingest(_fake_probe())
    assert plan.frame_rate == 25
    assert ["-g", "25"] == list(plan.video_args[plan.video_args.index("-g"):][:2])
    assert "-fps_mode" in plan.video_args and "cfr" in plan.video_args
    assert plan_ingest(_fake_probe(), IngestOptions(audio_mode="aac")).audio_action == "transcode"


def test_plan_handles_awkward_video():
    info = _fake_probe(width=1919, height=1081, field_order="tt", pix_fmt="yuvj420p",
                       r_frame_rate="60/1", avg_frame_rate="47/1")
    assert info.video.index == 0  # cover art is ignored
    plan = plan_ingest(info)
    assert plan.frame_rate == 60  # VFR: keep the frame-rate ceiling, drop nothing
    assert any(f.startswith("yadif") for f in plan.video_filters)
    assert any(f.startswith("crop=") for f in plan.video_filters)
    assert "scale=out_range=tv" in plan.video_filters


# ---------------- ingest (real ffmpeg) ----------------

@pytest.mark.parametrize("name,_kw,action", SOURCES, ids=[s[0] for s in SOURCES])
def test_ingest_preserves_frames_timing_and_audio(sources, tmp_path, name, _kw, action):
    src = sources[name]
    out = str(tmp_path / "video-1.mp4")
    result = ingest(src, out)
    info = probe(out)
    # an AAC copy becomes a lossless one where the installed ffmpeg reports priming a copy would play
    # (ffmpeg 9 does for the Matroska AAC here: 1024 samples, against timestamps of whole milliseconds)
    lossless_instead = action == "copy" and any("warm-up that the source skips" in w for w in result.warnings)
    action = "lossless" if lossless_instead else action
    assert result.ingest["audio"] == action
    assert info.video.codec == "h264" and info.video.pix_fmt == "yuv420p"
    assert not info.video.is_variable_rate
    assert frame_times(out)[0] == pytest.approx(0.0, abs=1e-6)  # frame 0 at t=0
    idx = frame_indices(out)
    assert len(idx) == result.frame_count
    if not any(k in name for k in ("vfr", "late", "burst")):
        assert idx == list(range(len(idx)))  # no dropped or duplicated frames
    if action == "none":
        assert info.audio is None
        return
    # the same source frame is on screen at every beep (audio/video sync preserved). Decodes are timed
    # from each file's start; a source with codec pre-roll keeps its own clock in the copy
    shift = probe(src).start_time if keeps_timestamps(probe(src)) else 0.0
    src_onsets, out_onsets = beep_onsets(audio_samples(src)), beep_onsets(audio_samples(out))
    assert len(out_onsets) == len(src_onsets) >= 3
    for s, o in zip(src_onsets, out_onsets):
        assert o == pytest.approx(s + shift, abs=0.002)
        if "vfr" not in name:
            assert _frame_at(out, o) == _frame_at(src, s)
    if "dualmono" in name or "pcm_stereo" in name:  # both sources carry identical channels
        assert probe(out).audio.channels == 1
    if action == "copy" or "pcm" in name:
        assert _same_pcm(src, out)  # bit-identical decoded audio
    elif action == "lossless":
        assert _max_float_diff(src, out) < 2e-7  # within 24-bit precision of the decoded source


def test_ingest_cancel_leaves_nothing(sources, tmp_path):
    out = tmp_path / "video-1.mp4"
    with pytest.raises(CancelledError):
        ingest(sources["vfr.mp4"], str(out), should_cancel=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_ingest_rejects_truncated_source(sources, tmp_path):
    good = sources["aac_mono_32k_30fps_fullrange.mp4"]
    cut = tmp_path / "cut.mp4"
    ff = find_toolchain().ffmpeg
    subprocess.run([ff, "-v", "error", "-i", good, "-c", "copy", "-movflags", "+faststart", str(cut)], check=True)
    data = cut.read_bytes()
    cut.write_bytes(data[: len(data) * 6 // 10])  # header intact, media data missing (partial copy/download)
    with pytest.raises(Exception, match="(?i)lasts|incomplete|invalid|damaged"):
        ingest(str(cut), str(tmp_path / "video-1.mp4"))
    assert not (tmp_path / "video-1.mp4").exists()


def test_ingest_rejects_files_without_video(tmp_path):
    ff = find_toolchain().ffmpeg
    audio_only = str(tmp_path / "a.m4a")
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", audio_only], check=True)
    with pytest.raises(Exception, match="no video"):
        ingest(audio_only, str(tmp_path / "video-1.mp4"))


def test_audio_analysis_detects_dual_mono_and_silence(sources):
    stats = analyze_audio(sources["mp3_dualmono_48k_25fps.mp4"], channels=2)
    assert stats.dual_mono is True
    assert stats.leading_silence_s == pytest.approx(0.5, abs=0.03)
    # 0.5-amplitude beeps upmixed mono->stereo at -3 dB per channel: about -9 dBFS peak
    assert -40 < (stats.loudness_lufs or 0) < -10 and stats.peak_dbfs == pytest.approx(-9.0, abs=1.0)


# ---------------- export ----------------

@pytest.fixture(scope="module")
def session(tmp_path_factory):
    root = tmp_path_factory.mktemp("root")
    sdir = root / "S1"
    sdir.mkdir()
    specs = [("video-1", dict(fps="25", audio=MP3, channels=2)),
             ("video-2", dict(fps="30", audio=AAC, sample_rate=32000))]
    videos = []
    for vid, kw in specs:
        src = make_media(str(root / f"{vid}-src.mp4"), duration=6, beep_every=0.37, beep_offset=0.11, **kw)
        r = ingest(src, str(sdir / f"{vid}.mp4"))
        videos.append(VideoItem(vid, f"{vid}.mp4", duration_ms=r.duration_ms, fps=float(r.frame_rate),
                                frame_rate=str(r.frame_rate), frame_count=r.frame_count, media=r.media))
    state = SessionState(root_dir=str(root), label="S1", session_dir=str(sdir), videos=videos)

    def rec(s, e, cam="video-1,video-2", audio="video-1"):  # frames as label.tsv numbers them (25 fps)
        return AnnotationRecord("S1", cam, 3, "Step", round(s * 25), round(e * 25), round(e * 25) - round(s * 25),
                                s, e, e - s, "video-1", audio, 5, "")

    state.annotations = [rec(0.0, 0.999), rec(1.013, 2.519), rec(2.5, 2.5), rec(4.987, 5.979, audio="video-2"),
                         rec(3.3, 4.1, cam="video-2")]
    return state


def test_export_clips_are_frame_exact_and_in_sync(session, tmp_path):
    report = export_segments(session, ExportOptions(out_dir=str(tmp_path)))
    assert report.failed == [] and report.warnings == []
    # one WAV per row, one MP4 per annotated camera (4 rows x 2 cameras + 1 row x 1 camera)
    assert len(report.results) == 5 + 4 * 2 + 1
    onsets = {v.video_id: beep_onsets(audio_samples(f"{session.session_dir}/{v.filename}")) for v in session.videos}
    for r in report.results:
        job = r.job
        if job.kind == "wav":
            with wave.open(job.out_path) as w:
                assert w.getnframes() == round(job.num_frames / float(job.fps) * w.getframerate())
                assert w.getnchannels() == 1  # mono source or exact dual-mono collapse
            continue
        assert frame_indices(job.out_path) == list(range(job.first_frame, job.last_frame + 1))
        expected = [t - job.start_s for t in onsets[job.video_id] if job.start_s <= t < job.end_s - 0.02]
        got = beep_onsets(audio_samples(job.out_path))
        assert got == pytest.approx(expected, abs=0.002)
    rows = open(report.manifest_csv, encoding="utf-8").read().splitlines()
    assert rows[0].startswith("session,row,step_no") and len(rows) == 1 + len(report.results)


def test_export_frame_ranges_follow_label_tsv(session, tmp_path):
    jobs, _ = plan_clips(session, ExportOptions(out_dir=str(tmp_path), write_wav=False))
    by_key = {(j.record_index, j.video_id): (j.first_frame, j.last_frame) for j in jobs}
    # times between frames (as 0.1.x stored them): the frames label.tsv lists, round(t * 25) = 25..63
    assert by_key[(1, "video-1")] == (25, 63)
    assert by_key[(1, "video-2")] == (30, 75)  # the 30 fps camera's frames shown at 1.000 s and 2.520 s
    assert by_key[(2, "video-1")] == (62, 62)  # zero-length label: a single frame


def test_export_command_seeks_before_and_trims_exactly(session, tmp_path):
    job = plan_clips(session, ExportOptions(out_dir=str(tmp_path)))[0][3]
    cmd = clip_command(job, ExportOptions(out_dir=str(tmp_path)))
    assert cmd[0] == "-ss" and float(cmd[1]) <= job.start_s


@pytest.mark.parametrize("expr,expected", [
    # full-wave buzz from 60 Hz mains: harmonics of 120 Hz, like the room2-4 recordings
    ("0.003*(sin(2*PI*120*t)+0.7*sin(2*PI*240*t)+0.5*sin(2*PI*360*t))", 120),
    # 50 Hz mains hum with odd harmonics
    ("0.003*(sin(2*PI*50*t)+0.6*sin(2*PI*150*t)+0.4*sin(2*PI*250*t))", 50),
    ("0", None),
])
def test_audio_analysis_detects_mains_hum(tmp_path, expr, expected):
    ff = find_toolchain().ffmpeg
    path = str(tmp_path / "hum.wav")
    # speech-like band-limited noise bursts plus the hum under test
    noise = "0.02*(random(0)-0.5)*gt(sin(2*PI*0.7*t),0)"
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc='{noise}+{expr}':s=16000:d=20", "-af", "lowpass=f=4000", path], check=True)
    stats = analyze_audio(path, channels=1)
    assert stats.hum_hz == expected
    assert (expected is None) == (not any("hum" in w for w in stats.warnings()))


def test_wav_export_rounds_without_bias(tmp_path):
    """True-stereo AAC (copied into the working copy) exported to 16-bit WAV must round, not truncate."""
    ff = find_toolchain().ffmpeg
    src = str(tmp_path / "stereo.mp4")
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25:d=3", "-f", "lavfi", "-i",
                    "aevalsrc='0.01*sin(2*PI*440*t)+0.003*(random(0)-0.5)|0.008*sin(2*PI*660*t)':s=48000:d=3",
                    "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k", "-shortest", src], check=True)
    sdir = tmp_path / "S"
    sdir.mkdir()
    r = ingest(src, str(sdir / "video-1.mp4"))
    item = VideoItem("video-1", "video-1.mp4", duration_ms=r.duration_ms, fps=25.0, frame_rate="25/1",
                     frame_count=r.frame_count, media=r.media)
    state = SessionState(root_dir=str(tmp_path), label="S", session_dir=str(sdir), videos=[item])
    state.annotations = [AnnotationRecord("S", "video-1", 1, "x", 0, 0, 0, 1.0, 1.96, 0.96, "video-1", "video-1")]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out"), write_video=False,
                                                  audio_channels="keep"))
    wav = report.results[0].job.out_path
    ref, got = array("f"), array("h")
    ref.frombytes(subprocess.run([ff, "-v", "error", "-i", str(sdir / "video-1.mp4"), "-af",
                                  "atrim=start=1:end=2,asetpts=PTS-STARTPTS", "-f", "f32le", "-"],
                                 capture_output=True, check=True).stdout)
    with wave.open(wav) as w:
        got.frombytes(w.readframes(w.getnframes()))
    n = min(len(ref), len(got))
    mean_err = sum(got[i] - ref[i] * 32768 for i in range(n)) / n
    assert abs(mean_err) < 0.05  # truncation would give about -0.5


def _single_video_session(tmp_path, src):
    sdir = tmp_path / "S"
    sdir.mkdir()
    r = ingest(src, str(sdir / "video-1.mp4"))
    item = VideoItem("video-1", "video-1.mp4", duration_ms=r.duration_ms, fps=float(r.frame_rate),
                     frame_rate=str(r.frame_rate), frame_count=r.frame_count, media=r.media)
    return SessionState(root_dir=str(tmp_path), label="S", session_dir=str(sdir), videos=[item])


def _rec(start, end, step=1, fps=25):  # frames as label.tsv numbers them
    first, last = round(start * fps), round(end * fps)
    return AnnotationRecord("S", "video-1", step, "x", first, last, last - first, start, end, end - start, "video-1",
                            "video-1")


def test_export_keeps_late_starting_audio_aligned(sources, tmp_path):
    # the audio track of this source starts 0.3 s after the video; its first beep is at 0.8 s
    state = _single_video_session(tmp_path, sources["audio_starts_late.mp4"])
    state.annotations = [_rec(0.0, 1.96)]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out")))
    assert report.failed == []
    timeline = beep_onsets(audio_samples(f"{state.session_dir}/video-1.mp4"))
    for r in report.results:  # the WAV starts where the audio does (on the frame the audio covers at least half of)
        expected = [t - r.job.start_s for t in timeline if r.job.start_s <= t < 2.0 - 0.02]
        assert beep_onsets(audio_samples(r.job.out_path)) == pytest.approx(expected, abs=0.002)
    starts = {r.job.kind: r.job.start_s for r in report.results}
    assert starts["video"] == 0.0 and abs(starts["wav"] - 0.3) <= 0.02
    # (the reported start can include the AAC encoder's priming, about 21 ms before the audible start)
    assert len(report.warnings) == 1 and re.fullmatch(
        r"row 1: the audio of video-1 starts at 0\.(2[789]\d|300) s; WAV starts at 0\.(280|320) s", report.warnings[0])


def test_reexport_removes_outputs_of_deleted_labels(sources, tmp_path):
    state = _single_video_session(tmp_path, sources["mp3_dualmono_48k_25fps.mp4"])
    out = tmp_path / "out"
    state.annotations = [_rec(0.0, 0.96, 1), _rec(1.0, 1.96, 2)]
    first = export_segments(state, ExportOptions(out_dir=str(out)))
    old_files = {r.job.out_path for r in first.results}
    state.annotations = [_rec(1.0, 1.96, 2)]  # label 1 deleted: label 2 is now row 1
    second = export_segments(state, ExportOptions(out_dir=str(out)))
    new_files = {r.job.out_path for r in second.results}
    on_disk = {str(p) for p in (out / "S").iterdir() if p.suffix in (".mp4", ".wav")}
    assert on_disk == new_files and old_files - new_files


def test_cancelled_reexport_leaves_the_previous_export_intact(sources, tmp_path):
    state = _single_video_session(tmp_path, sources["mp3_dualmono_48k_25fps.mp4"])
    out = tmp_path / "out"
    state.annotations = [_rec(0.0, 0.96, 1), _rec(1.0, 1.96, 2)]
    export_segments(state, ExportOptions(out_dir=str(out)))
    before = {p.name: p.read_bytes() for p in (out / "S").iterdir() if p.is_file()}
    state.annotations = [_rec(1.0, 1.96, 2)]  # label 1 deleted, then the re-export is cancelled
    polls = []
    with pytest.raises(CancelledError):
        export_segments(state, ExportOptions(out_dir=str(out)), should_cancel=lambda: len(polls.append(1) or polls) > 2)
    assert {p.name: p.read_bytes() for p in (out / "S").iterdir() if p.is_file()} == before
    assert not (out / "S" / ".export-in-progress").exists()


def test_ingest_accepts_a_silent_audio_track(tmp_path):
    ff = find_toolchain().ffmpeg
    src = str(tmp_path / "silent.mp4")
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25:d=2", "-f", "lavfi",
                    "-i", "anullsrc=r=48000:cl=stereo", "-t", "2", "-c:v", "libx264", "-c:a", "aac", src],
                   check=True)
    result = ingest(src, str(tmp_path / "video-1.mp4"))
    assert any("digital silence" in w for w in result.warnings)


def test_ingest_accepts_mkv_with_audio_longer_than_video(tmp_path):
    ff = find_toolchain().ffmpeg
    src = str(tmp_path / "obs.mkv")  # Matroska has no per-stream duration, only DURATION tags
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=30:d=3", "-f", "lavfi",
                    "-i", "sine=f=440:d=4", "-c:v", "libx264", "-c:a", "libopus", src], check=True)
    result = ingest(src, str(tmp_path / "video-1.mp4"))
    assert result.frame_count == 90 and abs(result.duration_ms - 3000) <= 40


@pytest.mark.parametrize("rate", ["15000/1001", "48000/1001", "120000/1001", "25/1", "30000/1001"])
def test_declared_constant_rates_are_kept_exactly(rate):
    assert plan_ingest(_fake_probe(r_frame_rate=rate, avg_frame_rate=rate)).frame_rate == Fraction(rate)


def test_videos_inferred_from_file_names_get_their_measured_rate(sources, tmp_path):
    from video_annote.core.storage import load_session_state
    from video_annote.sessions import refresh_video_facts

    sdir = tmp_path / "S"
    sdir.mkdir()
    ingest(sources["mp3_dualmono_48k_25fps.mp4"], str(sdir / "video-1.mp4"))
    (sdir / "session.json").write_text("{ truncated", encoding="utf-8")  # videos are inferred from files
    state, _ = load_session_state(str(tmp_path), "S")
    notes = refresh_video_facts(state)
    assert state.videos[0].exact_fps == Fraction(25) and state.videos[0].frame_count == 100
    assert not any("session.json said" in n for n in notes)  # nothing was stored to disagree with


# ---------------- round-2 media regressions ----------------

def _channel_tones(channels):
    return "|".join(f"0.3*sin(2*PI*{300 + 200 * i}*t)" for i in range(channels))


@pytest.mark.parametrize("layout,channels", [("2.1", 3), ("3c", 3), ("quad", 4), ("7.1", 8)])
def test_unusual_channel_layouts_keep_every_channel(tmp_path, layout, channels):
    # ALAC accepts one layout per channel count; anything else used to be remixed or dropped silently
    ff = find_toolchain().ffmpeg
    src = str(tmp_path / f"src_{channels}.mkv")
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25:d=2", "-f", "lavfi",
                    "-i", f"aevalsrc={_channel_tones(channels)}:s=48000:d=2", "-af",
                    f"pan={layout}|" + "|".join(f"c{i}=c{i}" for i in range(channels)),
                    "-c:v", "libx264", "-c:a", "pcm_s16le", src], check=True)
    out = str(tmp_path / "video-1.mp4")
    ingest(src, out)
    assert probe(out).audio.channels == channels

    def pcm(path):
        return subprocess.run([ff, "-v", "error", "-i", path, "-map", "0:a:0", "-f", "s16le", "-"],
                              capture_output=True, check=True).stdout

    assert pcm(out) == pcm(src)  # every channel, in order, sample for sample


def test_mp4_clip_audio_stays_exact_when_wavs_are_resampled(sources, tmp_path):
    state = _single_video_session(tmp_path, sources["aac_mono_32k_30fps_fullrange.mp4"])
    state.annotations = [_rec(0.0, 0.967, fps=30)]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out"), audio_sample_rate=16000))
    assert report.failed == []
    rates = {r.job.kind: probe(r.job.out_path).audio.sample_rate for r in report.results}
    assert rates == {"video": 32000, "wav": 16000}


def test_label_times_in_memory_map_like_stored_ones():
    from video_annote.core.timecode import recompute_from_times

    def item(vid, rate):
        return VideoItem(vid, f"{vid}.mp4", frame_rate=rate, frame_count=900, media={"audio": None})

    state = SessionState(label="S", session_dir="/nonexistent",
                         videos=[item("video-1", "30/1"), item("video-2", "30000/1001")])
    rec = AnnotationRecord("S", "video-1,video-2", 1, "x", 0, 0, 0, 7 / 30, 10 / 30, 0, "video-1", "video-1")
    state.annotations = [recompute_from_times(rec, Fraction(30))]  # as the GUI makes it
    assert state.annotations[0].start_time == 0.233  # the 3 decimals label.tsv stores
    jobs, _ = plan_clips(state, ExportOptions(out_dir="/nonexistent", write_wav=False))
    assert {j.video_id: (j.first_frame, j.last_frame) for j in jobs} == {"video-1": (7, 10), "video-2": (6, 9)}


def test_truncated_avi_is_rejected(tmp_path):
    from video_annote.media.ingest import IngestError

    ff = find_toolchain().ffmpeg
    src = tmp_path / "cam.avi"
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=6", "-f", "lavfi", "-i",
                    "sine=d=6", "-c:v", "mjpeg", "-c:a", "pcm_s16le", str(src)], check=True)
    data = src.read_bytes()
    src.write_bytes(data[: int(len(data) * 0.6)])  # an interrupted copy: the header still says 150 frames
    with pytest.raises(IngestError, match="truncated"):
        ingest(str(src), str(tmp_path / "video-1.mp4"))


def _ts_parts_with_gap(tmp_path, gap_s=1.0):
    ff = find_toolchain().ffmpeg
    parts = []
    for i, offset in enumerate((0.0, 3.0 + gap_s)):
        part = tmp_path / f"part{i}.ts"
        subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=3", "-c:v",
                        "libx264", "-g", "25", "-output_ts_offset", str(offset), "-f", "mpegts", str(part)], check=True)
        parts.append(part)
    return parts


def test_gaps_in_a_constant_rate_source_are_reported(tmp_path):
    src = tmp_path / "gap.ts"
    src.write_bytes(b"".join(p.read_bytes() for p in _ts_parts_with_gap(tmp_path)))
    result = ingest(str(src), str(tmp_path / "video-1.mp4"))
    counts = [int(m.group(1)) for w in result.warnings if (m := re.search(r"gaps \(about (\d+) frames", w))]
    assert counts and 20 <= counts[0] <= 26  # a 1 s gap at 25 fps


def test_a_stream_download_with_a_gap_is_rejected(tmp_path):
    # e.g. an HLS segment whose download stalled: ffmpeg skips the rest of it without saying so
    import functools
    import http.server
    import threading

    from video_annote.media.ingest import IngestError

    parts = _ts_parts_with_gap(tmp_path)  # segment 0 lost its last second: the playlist still counts it
    (tmp_path / "index.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:3.92,\n{parts[0].name}\n#EXTINF:3.0,\n{parts[1].name}\n#EXT-X-ENDLIST\n", encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(IngestError, match="missing"):
            ingest(f"http://127.0.0.1:{server.server_address[1]}/index.m3u8", str(tmp_path / "video-1.mp4"))
    finally:
        server.shutdown()


@pytest.mark.parametrize("level,line,converting,expected", [
    ("error", "[h264 @ 0x1] Invalid NAL unit size (22961 > 945).", True, "damaged"),
    ("error", "[opus @ 0x1] Error parsing Opus packet header.", True, None),  # ffmpeg 8.0, every MKV Opus
    ("warning", "[hls @ 0x1] Failed to open segment 3 of playlist 0", False, "lost"),
    ("error", "[http @ 0x1] HTTP error 404 Not Found", False, None),  # probing: an unused variant
    ("error", "[http @ 0x1] HTTP error 404 Not Found", True, "lost"),
    ("error", "[http @ 0x1] Stream ends prematurely at 393, should be 18446744073709551615", True, None),
    ("warning", "[hls @ 0x1] keepalive request failed for 'x' with error: 'Connection reset by peer' when "
                "opening url, retrying with new connection", True, None),
    ("info", "Input #0, mov,mp4, from 'corrupt HTTP error.mp4':", True, None),
])
def test_ffmpeg_log_classification(level, line, converting, expected):
    from video_annote.media.ffmpeg import classify_log_line

    assert classify_log_line(level, line, converting) == expected


# ---------------- round-3 regressions ----------------

def _serve(directory):
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_stream_whose_last_segment_is_cut_short_is_rejected(tmp_path):
    from video_annote.media.ingest import IngestError

    ff = find_toolchain().ffmpeg
    names = []
    for i in range(2):
        part = tmp_path / f"seg{i}.ts"
        subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=3", "-c:v", "libx264",
                        "-g", "25", "-output_ts_offset", str(3.0 * i), "-f", "mpegts", str(part)], check=True)
        names.append(part.name)
    data = (tmp_path / names[1]).read_bytes()
    (tmp_path / names[1]).write_bytes(data[: len(data) // 2 // 188 * 188])  # the download stopped halfway
    (tmp_path / "index.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:3\n#EXT-X-MEDIA-SEQUENCE:0\n"
        + "".join(f"#EXTINF:3.0,\n{n}\n" for n in names) + "#EXT-X-ENDLIST\n", encoding="utf-8")
    server = _serve(tmp_path)
    try:
        with pytest.raises(IngestError, match="incomplete"):
            ingest(f"http://127.0.0.1:{server.server_address[1]}/index.m3u8", str(tmp_path / "video-1.mp4"))
    finally:
        server.shutdown()


def _two_camera_session(tmp_path, a_kw, b_kw):
    sdir = tmp_path / "S"
    sdir.mkdir()
    items = []
    for vid, kw in (("video-1", a_kw), ("video-2", b_kw)):
        src = make_media(str(tmp_path / f"{vid}-src.mp4"), duration=4, **kw)
        r = ingest(src, str(sdir / f"{vid}.mp4"))
        items.append(VideoItem(vid, f"{vid}.mp4", duration_ms=r.duration_ms, fps=float(r.frame_rate),
                               frame_rate=str(r.frame_rate), frame_count=r.frame_count, media=r.media))
    return SessionState(root_dir=str(tmp_path), label="S", session_dir=str(sdir), videos=items)


def test_export_of_mixed_cameras(tmp_path):
    # time source 30 fps with dual-mono AAC (copied as is); audio source 25 fps; a camera without audio
    state = _two_camera_session(tmp_path, dict(fps="30", audio=AAC, channels=2), dict(fps="25", audio=MP3))
    state.videos.append(dataclasses.replace(state.videos[0], video_id="video-3", media={**state.videos[0].media,
                                                                                         "audio": None}))
    state.annotations = [AnnotationRecord("S", "video-1,video-3", 1, "x", 31, 61, 30, 31 / 30, 61 / 30, 1.0,
                                          "video-1", "video-2")]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out"), audio_channels="keep"))
    assert report.failed == [] and not any("audio channels" in w for w in report.warnings)
    by_kind = {(r.job.kind, r.job.video_id): r for r in report.results}
    wav = by_kind[("wav", "video-2")]
    # the WAV covers exactly the time-source frames 31..61 although its camera runs at 25 fps
    assert (wav.job.first_frame, wav.job.last_frame, wav.job.fps) == (31, 61, Fraction(30))
    assert probe(wav.job.out_path).audio.duration == pytest.approx(31 / 30, abs=0.001)
    # the MP4 clip stores an identical stereo pair once, whatever the WAV channel option says
    assert probe(by_kind[("video", "video-1")].job.out_path).audio.channels == 1
    assert probe(by_kind[("video", "video-3")].job.out_path).audio is None


def test_unlabelled_multichannel_audio_keeps_every_channel_in_a_mono_wav(tmp_path):
    ff = find_toolchain().ffmpeg
    src = str(tmp_path / "mics.mkv")
    tones = "|".join("0.3*sin(2*PI*900*t)" if i == 3 else "0" for i in range(6))  # only microphone 4
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25:d=2", "-f", "lavfi", "-i",
                    f"aevalsrc={tones}:s=48000:d=2", "-af", "pan=6c|" + "|".join(f"c{i}=c{i}" for i in range(6)),
                    "-c:v", "libx264", "-c:a", "pcm_s16le", src], check=True)
    state = _single_video_session(tmp_path, src)  # stored as 5.1: microphone 4 sits where LFE goes
    state.annotations = [_rec(0.0, 1.0)]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out"), audio_channels="mono",
                                                  write_video=False))
    wav = report.results[0].job.out_path
    assert report.failed == [] and probe(wav).audio.channels == 1
    assert max(abs(x) for x in audio_samples(wav)) > 0.01  # a standard downmix would drop the "LFE"


def test_aac_reencode_refuses_unlabelled_channels_it_would_treat_as_lfe(tmp_path):
    from video_annote.media.ingest import IngestError

    ff = find_toolchain().ffmpeg
    src = str(tmp_path / "mics.mkv")
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25:d=1", "-f", "lavfi", "-i",
                    "aevalsrc=" + "|".join(["0.1*sin(2*PI*500*t)"] * 6) + ":s=48000:d=1", "-af",
                    "pan=6c|" + "|".join(f"c{i}=c{i}" for i in range(6)), "-c:v", "libx264", "-c:a", "pcm_s16le",
                    src], check=True)
    with pytest.raises(IngestError, match="lossless"):
        ingest(src, str(tmp_path / "video-1.mp4"), IngestOptions(audio_mode="aac"))


# ---------------- round-4 regressions ----------------

def test_an_hls_stream_with_whole_second_segment_lengths_is_accepted(tmp_path):
    # protocol version 2 (and ffmpeg's -hls_flags round_durations) state each segment in whole seconds
    from video_annote.media.ingest import _hls_playlist

    ff = find_toolchain().ffmpeg
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=7.4", "-c:v", "libx264",
                    "-g", "25", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", "-hls_flags",
                    "round_durations", str(tmp_path / "index.m3u8")], check=True)
    assert "#EXTINF:1," in (tmp_path / "index.m3u8").read_text(encoding="utf-8")  # 1.4 s stated as 1
    (tmp_path / "master.m3u8").write_text(  # the variant of the imported resolution is read, not the largest
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nmissing.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=160x120\nindex.m3u8\n", encoding="utf-8")
    server = _serve(tmp_path)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        # 7.4 s in 4 segments stated as 2, 2, 2 and 1 s: each within half a second
        assert _hls_playlist(f"{url}/master.m3u8", probe(f"{url}/index.m3u8"), 30)[:3] == (7.0, 2.0, 1.0)
        result = ingest(f"{url}/master.m3u8", str(tmp_path / "video-1.mp4"))
    finally:
        server.shutdown()
    assert result.frame_count == 185
    assert any("rounded to 1 s, so its completeness could only be checked to within 2.1 s" in w for w in result.warnings)


def test_a_wav_is_never_padded_past_the_end_of_its_audio(tmp_path):
    # time source video-1: 25 fps, 4 s; audio source video-2: 30 fps, 3 s (PCM: its audio ends at exactly 3 s)
    sdir = tmp_path / "S"
    sdir.mkdir()
    items = []
    for vid, ext, kw in (("video-1", "mp4", dict(duration=4, fps="25", audio=None)),
                         ("video-2", "mov", dict(duration=3, fps="30", audio=("-c:a", "pcm_s16le")))):
        r = ingest(make_media(str(tmp_path / f"{vid}-src.{ext}"), **kw), str(sdir / f"{vid}.mp4"))
        items.append(VideoItem(vid, f"{vid}.mp4", duration_ms=r.duration_ms, fps=float(r.frame_rate),
                               frame_rate=str(r.frame_rate), frame_count=r.frame_count, media=r.media))
    state = SessionState(root_dir=str(tmp_path), label="S", session_dir=str(sdir), videos=items)
    state.annotations = [AnnotationRecord("S", "video-1", 1, "x", 50, 99, 49, 2.0, 3.96, 1.96, "video-1", "video-2"),
                         AnnotationRecord("S", "video-1", 1, "x", 88, 99, 11, 3.52, 3.96, 0.44, "video-1", "video-2")]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out"), write_video=False))
    assert report.failed == [] and len(report.results) == 1
    wav = report.results[0]
    assert (wav.job.first_frame, wav.job.last_frame) == (50, 74)  # stops at 3.000 s, not padded to 4.000 s
    assert probe(wav.job.out_path).audio.duration == pytest.approx(1.0, abs=0.001)
    assert report.warnings == ["row 1: the audio of video-2 ends at 3.000 s; WAV truncated at 3.000 s",
                               "row 2: the audio of video-2 ends at 3.000 s, before this annotation starts; WAV skipped"]


def test_a_wav_beyond_its_time_source_names_the_time_source(tmp_path):
    state = _two_camera_session(tmp_path, dict(fps="25", audio=None), dict(fps="30", audio=MP3))
    state.videos[0] = dataclasses.replace(state.videos[0], frame_count=75)  # the time source ends at 3.0 s
    state.annotations = [AnnotationRecord("S", "video-1", 1, "x", 50, 99, 49, 2.0, 3.96, 1.96, "video-1", "video-2")]
    jobs, warnings = plan_clips(state, ExportOptions(out_dir=str(tmp_path / "out"), write_video=False))
    assert [(j.first_frame, j.last_frame) for j in jobs] == [(50, 74)]
    assert warnings == ["row 1: video-1 (the time source) ends at 3.000 s; WAV truncated at 3.000 s"]


def test_a_start_up_burst_neither_hides_gaps_nor_counts_as_lost_frames(tmp_path):
    from video_annote.media.ffmpeg import FFmpegResult
    from video_annote.media.ingest import _first_frame_times, _merged_at_start, _missing_frames, _validate

    src = make_media(str(tmp_path / "burst.mp4"), duration=4, fps="30", audio=None, merged_start=8)
    info = probe(src)
    # a real camera's few merged frames hardly change its average rate (13983 frames: 0.01 %); in a
    # 4 s file they do, so treat it as the constant-rate file it stands for
    info = dataclasses.replace(info, video=dataclasses.replace(info.video, avg_frame_rate=Fraction(30)))
    plan = plan_ingest(info)
    merged = _merged_at_start(_first_frame_times(src, info, 60), info, plan.frame_rate)
    assert merged == 7
    gap = FFmpegResult("", filter_dups=6, filter_drops=7)  # a 6-frame gap later in the recording
    assert _missing_frames(info, plan, gap, merged) == 6 and _missing_frames(info, plan, gap) == 0
    copy = dataclasses.replace(info, video=dataclasses.replace(info.video, frame_count=info.video.frame_count - 7))
    assert not any("frames" in w for w in _validate(info, copy, plan, merged))
    assert any("frames" in w for w in _validate(info, copy, plan))  # what round 4 reported on real cameras


def test_videos_after_an_unreadable_one_are_still_measured(sources, tmp_path):
    from video_annote.core.storage import load_session_state
    from video_annote.sessions import refresh_video_facts

    sdir = tmp_path / "S"
    sdir.mkdir()
    (sdir / "video-1.mp4").write_bytes(b"not a video")
    ingest(sources["mp3_dualmono_48k_25fps.mp4"], str(sdir / "video-2.mp4"))
    state, _ = load_session_state(str(tmp_path), "S")  # inferred from the file names
    notes = refresh_video_facts(state)
    assert not state.videos[0].rate_known and state.videos[1].exact_fps == Fraction(25)
    assert any(n.startswith("video-1: video-1.mp4 could not be read") for n in notes)



# ---------------- round-5 regressions ----------------

def test_a_wav_clamped_at_both_ends_gets_one_warning_per_edge(tmp_path):
    state = _two_camera_session(tmp_path, dict(fps="25", audio=None), dict(fps="30", audio=MP3))
    state.videos[0] = dataclasses.replace(state.videos[0], frame_count=100)  # the time source ends at 4.0 s
    state.videos[1].media["audio"].update(start_s=0.5, end_s=3.0)
    state.annotations = [AnnotationRecord("S", "video-1", 1, "x", 0, 0, 0, 0.0, 4.96, 4.96, "video-1", "video-2"),
                         AnnotationRecord("S", "video-1", 1, "x", 0, 0, 0, 3.52, 4.96, 1.44, "video-1", "video-2"),
                         AnnotationRecord("S", "video-1", 1, "x", 0, 0, 0, 0.0, 0.2, 0.2, "video-1", "video-2")]
    jobs, warnings = plan_clips(state, ExportOptions(out_dir=str(tmp_path / "out"), write_video=False))
    assert [(j.first_frame, j.last_frame) for j in jobs] == [(12, 74)]  # 0.48-3.0 s: the frames the audio covers
    assert warnings == ["row 1: the audio of video-2 starts at 0.500 s; WAV starts at 0.480 s",
                        "row 1: the audio of video-2 ends at 3.000 s; WAV truncated at 3.000 s",
                        "row 2: the audio of video-2 ends at 3.000 s, before this annotation starts; WAV skipped",
                        "row 3: the audio of video-2 starts at 0.500 s, after this annotation ends; WAV skipped"]


def test_a_burst_and_a_gap_do_not_add_a_frame_count_warning(tmp_path):
    from video_annote.media.ingest import _validate

    src = make_media(str(tmp_path / "burst.mp4"), duration=4, fps="30", audio=None, merged_start=8)
    info = probe(src)
    info = dataclasses.replace(info, video=dataclasses.replace(info.video, avg_frame_rate=Fraction(30)))
    copy = dataclasses.replace(info, video=dataclasses.replace(info.video, frame_count=info.video.frame_count - 1))
    # 7 start-up frames merged, 6 frames repeated to fill a gap: one frame fewer, all explained
    assert not any("frames" in w for w in _validate(info, copy, plan_ingest(info), merged=7, gap=6))


def _hls_with_ntsc_segments(tmp_path, stated):
    ff = find_toolchain().ffmpeg
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=30000/1001:d=24.024",
                    "-c:v", "libx264", "-g", "180", "-keyint_min", "180", "-sc_threshold", "0", "-f", "hls",
                    "-hls_time", "6", "-hls_playlist_type", "vod", str(tmp_path / "index.m3u8")], check=True)
    playlist = (tmp_path / "index.m3u8").read_text(encoding="utf-8")
    (tmp_path / "index.m3u8").write_text(re.sub(r"#EXTINF:[\d.]+,", f"#EXTINF:{stated},", playlist), encoding="utf-8")


def test_an_hls_playlist_stating_the_target_length_is_accepted(tmp_path):
    # packagers often write 6.000 for 6.006 s segments at 29.97 fps: the complete copy is longer than stated
    _hls_with_ntsc_segments(tmp_path, "6.000")
    server = _serve(tmp_path)
    try:
        result = ingest(f"http://127.0.0.1:{server.server_address[1]}/index.m3u8", str(tmp_path / "video-1.mp4"))
    finally:
        server.shutdown()
    assert result.frame_count == 720


def test_a_0_1_x_export_skips_only_the_outputs_of_an_unreadable_video(sources, tmp_path):
    state = _single_video_session(tmp_path, sources["mp3_dualmono_48k_25fps.mp4"])
    state.videos[0].media = {}  # as 0.1.x stored it: measured at export time
    (tmp_path / "S" / "video-2.mp4").write_bytes(b"not a video")
    state.videos.append(VideoItem("video-2", "video-2.mp4", fps=25.0))
    state.annotations = [AnnotationRecord("S", "video-1,video-2", 1, "x", 0, 0, 0, 0.0, 0.96, 0.96, "video-1", "video-1")]
    jobs, warnings = plan_clips(state, ExportOptions(out_dir=str(tmp_path / "out")))
    assert sorted((j.kind, j.video_id) for j in jobs) == [("video", "video-1"), ("wav", "video-1")]
    assert any(w.startswith("video-2: video-2.mp4 could not be read") for w in warnings)


def test_an_export_reports_labels_whose_frames_do_not_match_their_times(sources, tmp_path):
    state = _single_video_session(tmp_path, sources["mp3_dualmono_48k_25fps.mp4"])
    state.annotations = [AnnotationRecord("S", "video-1", 1, "x", 50, 100, 50, 1.0, 2.0, 1.0, "video-1", "video-1")]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out"), write_wav=False))
    assert report.failed == [] and (report.results[0].job.first_frame, report.results[0].job.last_frame) == (25, 50)
    assert any(w.startswith("label.tsv: the frame numbers of 1 label(s) (rows 1)") for w in report.warnings)
    assert state.annotations[0].start_frame == 50  # the caller's records are not changed


# ---------------- round-6 regressions ----------------

def test_the_timestamps_cadence_settles_disagreeing_stated_rates():
    from video_annote.media.ingest import _cadence

    burst = [i * 0.000011 for i in range(8)] + [(i - 7) / 30 for i in range(8, 40)]
    assert _cadence(burst) == 30  # a start-up burst aside, the frames follow 30 fps
    assert _cadence([i * 1001 / 30000 for i in range(40)]) == Fraction(30000, 1001)
    assert _cadence([i / 30 + (0.015 if i % 2 else 0) for i in range(40)]) is None  # irregular
    # a camera's start-up burst read through HLS: ffprobe states 60 (r) and 30 (average)
    hls = plan_ingest(_fake_probe(r_frame_rate="60/1", avg_frame_rate="30/1"), cadence=Fraction(30))
    assert hls.frame_rate == 30 and any("taken from the frame timestamps" in n for n in hls.notes)
    # a screen recording really is variable: up to 60 fps, 42 on average, frames 1/60 s apart
    screen = plan_ingest(_fake_probe(r_frame_rate="60/1", avg_frame_rate="42/1"), cadence=Fraction(60))
    assert screen.frame_rate == 60
    ntsc = plan_ingest(_fake_probe(r_frame_rate="30000/1001", avg_frame_rate="30000/1001"), cadence=Fraction(30))
    assert ntsc.frame_rate == Fraction(30000, 1001)  # (a cadence within 2 % never overrides)
    unstated = plan_ingest(_fake_probe(r_frame_rate="0/0", avg_frame_rate="0/0"), cadence=Fraction(25))
    assert unstated.frame_rate == 25 and not any("assumed 30" in n for n in unstated.notes)


def test_an_hls_master_is_checked_against_the_variant_that_is_imported(tmp_path):
    # an audio-only rendition listed first (its audio runs longer than the video) must not make a complete
    # video variant look short
    ff = find_toolchain().ffmpeg
    src = make_media(str(tmp_path / "src.mp4"), duration=8, fps="25", audio=AAC, size="160x120")
    for name, maps in (("av", ["-map", "0:v", "-map", "0:a"]), ("aud", ["-map", "0:a"])):
        subprocess.run([ff, "-v", "error", "-i", src, "-t", "8" if name == "av" else "8.25", *maps, "-c:v", "libx264",
                        "-g", "25", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                        "-hls_segment_filename", str(tmp_path / f"{name}%d.ts"), str(tmp_path / f"{name}.m3u8")],
                       check=True)
    (tmp_path / "master.m3u8").write_text(
        '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\naud.m3u8\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=160x120\nav.m3u8\n', encoding="utf-8")
    server = _serve(tmp_path)
    try:
        result = ingest(f"http://127.0.0.1:{server.server_address[1]}/master.m3u8", str(tmp_path / "video-1.mp4"))
    finally:
        server.shutdown()
    assert 200 <= result.frame_count <= 202  # accepted (MPEG-TS audio priming can add a lead-in frame or two)


def test_a_two_frame_gap_still_shows_in_the_frame_count(tmp_path):
    from video_annote.media.ingest import _validate

    info = _fake_probe(nb_frames="250", duration="10.0")
    copy = dataclasses.replace(info, video=dataclasses.replace(info.video, frame_count=252, duration=10.08))
    assert any("252 frames, the source 250" in w for w in _validate(info, copy, plan_ingest(info)))


def test_a_wav_whose_label_has_no_audio_is_skipped(tmp_path):
    state = _two_camera_session(tmp_path, dict(fps="25", audio=None), dict(fps="30", audio=MP3))
    state.videos[0] = dataclasses.replace(state.videos[0], frame_count=75)  # the time source ends at 3.0 s
    state.videos[1].media["audio"].update(start_s=3.5, end_s=4.0)
    state.annotations = [AnnotationRecord("S", "video-1", 1, "x", 50, 100, 50, 2.0, 4.0, 2.0, "video-1", "video-2")]
    jobs, warnings = plan_clips(state, ExportOptions(out_dir=str(tmp_path / "out"), write_video=False))
    assert jobs == [] and warnings == ["row 1: the audio of video-2 starts at 3.500 s and video-1 (the time source) "
                                       "ends at 3.000 s; WAV skipped"]


def test_quiet_16_bit_audio_is_not_taken_for_mains_hum(tmp_path):
    ff = find_toolchain().ffmpeg
    path = str(tmp_path / "quiet.mov")
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25:d=30", "-f", "lavfi", "-i",
                    "aevalsrc='0.003*(random(0)-0.5)':s=48000:d=30", "-c:v", "libx264", "-c:a", "pcm_s16le", path],
                   check=True)
    assert analyze_audio(path, channels=1).hum_hz is None


# ---------------- round-7 regressions ----------------

def test_burst_cameras_read_through_hls_keep_their_rate_and_their_gap_checks():
    from video_annote.media.ingest import _constant_rate

    # ffprobe 4.4 states only the doubled rate (no average) for such a stream
    old = plan_ingest(_fake_probe(r_frame_rate="60/1", avg_frame_rate="0/0"), cadence=Fraction(30))
    assert old.frame_rate == 30 and old.timed
    # a short excerpt of a burst camera: its average is off by more than the snapping tolerance
    short = plan_ingest(_fake_probe(r_frame_rate="30/1", avg_frame_rate="3035/100"), cadence=Fraction(30))
    assert short.frame_rate == 30 and short.timed
    # frames repeated to fill such a stream mean lost frames (a stalled segment), as for any camera ...
    hls = _fake_probe(r_frame_rate="60/1", avg_frame_rate="30/1")
    assert _constant_rate(hls, plan_ingest(hls, cadence=Fraction(30)), burst=7)
    fmp4 = _fake_probe(r_frame_rate="30/1", avg_frame_rate="31/1")
    assert _constant_rate(fmp4, plan_ingest(fmp4, cadence=Fraction(30)), burst=0)
    # ... but not for a truly variable-rate source such as a screen recording
    screen = _fake_probe(r_frame_rate="60/1", avg_frame_rate="42/1")
    assert not _constant_rate(screen, plan_ingest(screen, cadence=Fraction(60)), burst=0)


def test_an_hls_master_is_followed_to_the_variant_the_probe_picks(tmp_path):
    from video_annote.media.ingest import _hls_playlist

    for name, extinf in (("first", "2.000000"), ("second", "2"), ("audio", "2.000")):
        (tmp_path / f"{name}.m3u8").write_text(f"#EXTM3U\n#EXTINF:{extinf},\na.ts\n#EXT-X-ENDLIST\n", encoding="utf-8")
    info = _fake_probe(width=320, height=240)
    master = tmp_path / "master.m3u8"
    master.write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x240\nfirst.m3u8\n'
                      '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x240\nsecond.m3u8\n', encoding="utf-8")
    assert _hls_playlist(str(master), info, 5)[:3] == (2.0, 0.0000005, 0.000001)  # the first of equal resolutions
    master.write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=9000000,CODECS="mp4a.40.2"\naudio.m3u8\n'
                      '#EXT-X-STREAM-INF:BANDWIDTH=300000\nsecond.m3u8\n', encoding="utf-8")
    assert _hls_playlist(str(master), info, 5)[:3] == (2.0, 0.5, 1.0)  # not the audio-only rendition


# ---------------- recording times (real-world clock) ----------------

def test_stated_times_are_parsed_with_their_precision():
    from datetime import datetime, timezone

    from video_annote.media.recorded import iso_utc, parse_timestamp

    utc = timezone.utc
    assert parse_timestamp("2026-03-20T18:21:23.203735Z") == (datetime(2026, 3, 20, 18, 21, 23, 203735, utc), 1e-6,
                                                              "", False)
    # ffprobe prints an MP4 creation time (whole seconds) with six zero decimals
    assert parse_timestamp("2026-03-20T16:37:15.000000Z", whole_if_zero=True).precision_s == 1.0
    assert parse_timestamp("2026-03-20T11:37:15-0500") == (datetime(2026, 3, 20, 16, 37, 15, tzinfo=utc), 1.0,
                                                           "-05:00", False)
    assert parse_timestamp("2026-03-20T18:21:23.120+00:00")[1:] == (0.001, "+00:00", False)
    assert parse_timestamp("2026:03:20 17:21:23+01:00").when == datetime(2026, 3, 20, 16, 21, 23, tzinfo=utc)  # EXIF
    for unusable in ("1970-01-01T00:00:00.000000Z", "1904-01-01T00:00:00Z",  # clocks never set
                     "2026-03-20 18:21:23",  # no time zone (unless asked for)
                     "2026-13-01T00:00:00Z", "2026-03-20", "20261026152732", "", None):
        assert parse_timestamp(unusable) is None, unusable
    assert iso_utc(datetime(2026, 3, 20, 16, 37, 15, tzinfo=utc)) == "2026-03-20T16:37:15Z"


def test_typed_unix_timestamps_are_read_in_their_unit():
    from datetime import datetime, timezone

    from video_annote.media.recorded import parse_unix_time

    utc = timezone.utc
    assert parse_unix_time("1774024635") == (datetime(2026, 3, 20, 16, 37, 15, tzinfo=utc), 1.0, "seconds")
    assert parse_unix_time(" 1774024635.203 ")[1:] == (0.001, "seconds")
    assert parse_unix_time("1774024635203") == (datetime(2026, 3, 20, 16, 37, 15, 203000, utc), 0.001, "milliseconds")
    assert parse_unix_time("1774024635203735") == (datetime(2026, 3, 20, 16, 37, 15, 203735, utc), 1e-6,
                                                   "microseconds")
    assert parse_unix_time("1774024635203735999").when.microsecond == 203736  # nanoseconds, to the microsecond
    for bad in ("", "abc", "-5", "1.2.3", "12345", "1e9", "177402463520373500000"):  # not a time, 1970, too long
        with pytest.raises(ValueError):
            parse_unix_time(bad)


def test_typed_dates_and_times_are_read_as_recording_software_writes_them(chicago):
    from datetime import datetime, timezone

    from video_annote.media.recorded import EnteredTime, forms_that_read, read_entered

    def utc(text, form="year-first", zone="America/New_York"):
        t = read_entered(EnteredTime(text, form, zone))
        return t.when.astimezone(timezone.utc).replace(tzinfo=None), t.precision_s

    nvr = datetime(2026, 10, 26, 19, 27, 32)  # 15:27:32 US Eastern (UTC-4 in October)
    for text in ("20261026152732", "20261026_152732", "20261026-152732", "20261026T152732",  # NVRs, Teams, Android
                 "2026-10-26 15:27:32", "2026-10-26T15:27:32", "2026-10-26 15-27-32", "2026-10-26_15-27-32",  # OBS
                 "2026-10-26 15.27.32", "2026:10:26 15:27:32", "2026/10/26 15:27:32",  # Zoom, EXIF
                 "2026-10-26 at 3.27.32 PM", "26 Oct 2026 15:27:32", "Monday, October 26, 2026 at 3:27:32 PM"):
        assert utc(text) == (nvr, 1.0), text
    assert utc("20261026152732123") == (nvr.replace(microsecond=123000), 0.001)  # Hikvision: milliseconds appended
    assert utc("2026-10-26 15:27:32.203735") == (nvr.replace(microsecond=203735), 1e-6)
    assert utc("2026-12-26 15:27:32")[0] == datetime(2026, 12, 26, 20, 27, 32)  # UTC-5 in December
    assert utc("2026-10-26 15:27:32", zone="")[0] == datetime(2026, 10, 26, 20, 27, 32)  # this computer's (Chicago)
    for typed in ("2026-10-26T15:27:32-04:00", "2026-10-26 19:27:32Z", "2026-10-26 19:27:32 UTC",
                  "Mon Oct 26 2026 15:27:32 GMT-0400 (Eastern Daylight Time)", "Mon, 26 Oct 2026 19:27:32 +0000",
                  "2026-10-26 14:27:32 UTC−05:00"):  # (− as the app prints offsets)
        t = read_entered(EnteredTime(typed, "month-first", "Asia/Tokyo"))  # a zone typed wins over the one chosen
        assert t.when == nvr.replace(tzinfo=timezone.utc) and t.zone_typed, typed
    assert utc("10/11/2026 15:27:32", "month-first")[0].month == 10
    assert utc("10/11/2026 15:27:32", "day-first")[0].month == 11
    assert utc("10/26/2026 3:27:32 PM", "month-first") == utc("26.10.2026 15:27:32", "day-first") == (nvr, 1.0)

    said = [read_entered(EnteredTime(*e)).said for e in (
        ("1774024635.203",), ("20261026152732", "year-first", "America/New_York"),
        ("10/26/2026 3:27:32 PM", "month-first", "Europe/London"), ("2026-10-26 15:27:32", "day-first", ""),
        ("2026-10-26T15:27:32-04:00", "year-first", "UTC"))]
    assert said == ["Unix time 1774024635.203", "20261026152732, US Eastern time",
                    "10/26/2026 3:27:32 PM as month/day/year, Europe/London time",
                    "2026-10-26 15:27:32, this computer's time zone", "2026-10-26T15:27:32-04:00"]

    for text, form, problem in (("2026-10-26 15:27", "year-first", "Type the date and time like"),
                                ("2026-13-26 15:27:32", "year-first", "no such date"),
                                ("2026-10-26 13:27:32 PM", "year-first", "no such date"),
                                ("26/10/2026 15:27:32", "month-first", "no such date"),
                                ("2026-10-26 15:27:32 EDT", "year-first", "Leave out EDT and choose the time zone"),
                                ("1985-10-26 15:27:32", "year-first", "before 1990"),
                                ("1793042852", "year-first", "Type the date and time like")):
        with pytest.raises(ValueError, match=problem):
            read_entered(EnteredTime(text, form, "UTC"))
    with pytest.raises(ValueError, match="Unknown time zone"):
        read_entered(EnteredTime("2026-10-26 15:27:32", "year-first", "Mars/Olympus"))
    assert forms_that_read("1793042852") == ["unix"]
    assert forms_that_read("20261026152732") == ["year-first", "month-first", "day-first"]  # not Unix ms: year 2612
    assert forms_that_read("26/10/2026 15:27:32") == ["day-first"]


def test_a_time_without_a_zone_is_read_as_this_computers(chicago):
    from datetime import datetime, timezone

    from video_annote.media.recorded import find_recording_time, parse_timestamp, recorded_block

    parsed = parse_timestamp("2026-10-26 15:27:32", local_if_no_zone=True)  # as AVI files state it
    assert parsed == (datetime(2026, 10, 26, 20, 27, 32, tzinfo=timezone.utc), 1.0, "-05:00", True)
    info = _fake_probe(tags={"creation_time": "2026-10-26T20:27:33.000000Z"})
    info.raw["format"]["tags"] = {"creation_time": "2026-10-26 15:27:32"}
    assert find_recording_time("x.avi", info).source == "the file's creation time (video track)"  # a stated zone wins
    del info.raw["streams"][1]["tags"]
    stamp = find_recording_time("x.avi", info)
    assert (stamp.source, stamp.assumed_zone) == ("the file's creation time", True)
    block = recorded_block(stamp, 0.0, 0.0, 2.0)
    start = int(datetime(2026, 10, 26, 20, 27, 32, tzinfo=timezone.utc).timestamp())
    assert block == {"start": "2026-10-26T15:27:32-05:00", "end": "2026-10-26T15:27:34-05:00",
                     "start_unix": start, "end_unix": start + 2,
                     "from": "the file's creation time, to the second (no time zone given: read as this computer's "
                             "local time)"}


def test_the_most_precise_stated_time_is_used():
    from video_annote.media.recorded import find_recording_time

    info = _fake_probe(tags={"creation_time": "2026-03-20T16:37:16.000000Z"})  # a track's time only
    stamp = find_recording_time("x.mp4", info)
    assert (stamp.source, stamp.precision_s) == ("the file's creation time (video track)", 1.0)
    info.raw["format"]["tags"] = {"creation_time": "2026-03-20T16:37:15.000000Z", "encoder": "Lavf"}
    stamp = find_recording_time("x.mp4", info)
    assert stamp.source == "the file's creation time" and stamp.when.second == 15
    info.raw["format"]["tags"]["com.apple.quicktime.creationdate"] = "2026-03-20T11:37:14-0500"  # iPhone
    stamp = find_recording_time("x.mp4", info)
    assert (stamp.source, stamp.when.hour) == ("the file's creation date", 16)
    stamp = find_recording_time("x.mp4", info, "2026-03-20T16:37:14.250Z")  # an HLS stream's date
    assert (stamp.precision_s, stamp.at_first_sample) == (0.001, True)
    assert find_recording_time("x.mp4", _fake_probe()) is None


@pytest.mark.parametrize("name,metadata,start,source", [
    # macOS screen recordings, MP4 writers: the movie header (UTC, whole seconds)
    ("screen.mov", ("-metadata", "creation_time=2026-03-20T16:37:15Z"), "2026-03-20T11:37:15-05:00",
     "the file's creation time, to the second"),
    # iPhone/QuickTime: local time with its offset, in the QuickTime metadata keys
    ("phone.mov", ("-metadata", "com.apple.quicktime.creationdate=2026-03-20T11:37:15-0500",
                   "-movflags", "use_metadata_tags"), "2026-03-20T11:37:15-05:00",
     "the file's creation date, to the second"),
    # Matroska/WebM writers (OBS when set, mkvmerge): microseconds
    ("obs.mkv", ("-metadata", "creation_time=2026-03-20T16:37:15.123456Z"), "2026-03-20T11:37:15.123456-05:00",
     "the file's creation time, to the microsecond"),
    # MP4 ©day with an offset
    ("tool.mp4", ("-metadata", "date=2026-03-20T11:37:15-05:00"), "2026-03-20T11:37:15-05:00",
     "the file's date, to the second"),
    # a year alone is no recording time
    ("year.mp4", ("-metadata", "date=2026"), None, "not stated in the file"),
])
def test_recording_times_of_different_writers(tmp_path, chicago, name, metadata, start, source):
    src = make_media(str(tmp_path / name), duration=1, audio=AAC, extra=metadata)
    rec = ingest(src, str(tmp_path / "video-1.mp4")).recorded
    assert (rec["start"], rec["from"]) == (start, source)


def test_import_keeps_the_recording_time(tmp_path, chicago):
    src = make_media(str(tmp_path / "cam.mp4"), duration=2, extra=("-metadata", "creation_time=2025-01-02T03:04:05Z"))
    result = ingest(src, str(tmp_path / "video-1.mp4"))
    assert result.recorded == {"start": "2025-01-01T21:04:05-06:00", "end": "2025-01-01T21:04:07-06:00",
                               "start_unix": 1735787045, "end_unix": 1735787047,
                               "from": "the file's creation time, to the second"}
    # ffmpeg does not carry it into the working copy by itself: the copy keeps it too (to the second)
    assert probe(str(tmp_path / "video-1.mp4")).raw["format"]["tags"]["creation_time"].startswith("2025-01-02T03:04:05")

    bare = ingest(make_media(str(tmp_path / "bare.mp4"), duration=1, audio=None), str(tmp_path / "video-2.mp4"))
    assert bare.recorded == {"start": None, "from": "not stated in the file"}


def test_a_tobii_recording_states_its_start_to_the_microsecond(tmp_path, chicago):
    import json

    folder = tmp_path / "20250409T021335Z"
    folder.mkdir()
    # like the glasses: MP3 audio from 0, video from 0.0424 s, whole-second creation time before the start
    src = make_media(str(folder / "scenevideo.mp4"), duration=3, audio=MP3, sample_rate=24000, video_delay=0.0424,
                     extra=("-metadata", "creation_time=2025-04-09T02:13:34Z"))
    info = probe(src)
    end = info.video.start_time + info.video.duration
    g3 = {"name": "Recording 4", "created": "2025-04-09T02:13:35.103790Z", "timezone": "America/Los_Angeles",
          "duration": end, "scenecamera": {"file": "scenevideo.mp4"}}
    (folder / "recording.g3").write_text(json.dumps(g3), encoding="utf-8")
    rec = ingest(src, str(tmp_path / "video-1.mp4")).recorded
    assert (rec["start"], rec["from"]) == ("2025-04-08T21:13:35.103790-05:00",
                                           "the Tobii recording file (recording.g3), to the microsecond")
    # a recording.g3 of another recording (other length, or naming another file) is not used
    for changed in ({"duration": end + 5}, {"scenecamera": {"file": "other.mp4"}}):
        (folder / "recording.g3").write_text(json.dumps({**g3, **changed}), encoding="utf-8")
        rec = ingest(src, str(tmp_path / "video-2.mp4")).recorded
        assert (rec["start"], rec["from"]) == ("2025-04-08T21:13:34-05:00", "the file's creation time, to the second")


def test_an_hls_program_date_time_dates_the_stream(tmp_path, chicago):
    from datetime import datetime, timezone

    from video_annote.media.ingest import _hls_playlist

    ff = find_toolchain().ffmpeg
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=4", "-c:v", "libx264",
                    "-g", "25", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                    str(tmp_path / "index.m3u8")], check=True)
    playlist = (tmp_path / "index.m3u8").read_text(encoding="utf-8")
    playlist = playlist.replace("#EXTINF:", "#EXT-X-PROGRAM-DATE-TIME:2026-04-03T14:19:26.250Z\n#EXTINF:", 1)
    (tmp_path / "index.m3u8").write_text(playlist, encoding="utf-8")
    server = _serve(tmp_path)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/index.m3u8"
        assert _hls_playlist(url, probe(url), 30).program_date_time == "2026-04-03T14:19:26.250Z"
        result = ingest(url, str(tmp_path / "video-1.mp4"))
    finally:
        server.shutdown()
    start = datetime(2026, 4, 3, 14, 19, 26, 250000, timezone.utc).timestamp()
    assert result.recorded == {"start": "2026-04-03T09:19:26.250-05:00", "end": "2026-04-03T09:19:30.250-05:00",
                               "start_unix": start, "end_unix": start + 4,
                               "from": "the stream's program date-time, to the millisecond"}


def test_sessions_imported_before_get_their_recording_time_on_opening(tmp_path):
    from video_annote.sessions import SourceSpec, create_session, refresh_video_facts

    src = make_media(str(tmp_path / "cam.mp4"), duration=2, audio=None,
                     extra=("-metadata", "creation_time=2025-01-02T03:04:05Z"))
    state = create_session(str(tmp_path / "root"), "S", [SourceSpec("video-1", src, "local")]).state
    expected = state.videos[0].recorded
    assert expected["start_unix"] == 1735787045
    state.videos[0].recorded = {}  # as stored before this version
    refresh_video_facts(state)
    assert state.videos[0].recorded == expected  # read from the recording it was imported from

    old = make_media(str(tmp_path / "old.mp4"), duration=2, audio=None)  # a working copy that states nothing
    other = make_media(str(tmp_path / "other.mp4"), duration=3, audio=None,
                       extra=("-metadata", "creation_time=2020-01-01T00:00:00Z"))
    state.videos[0].recorded = {}
    state.videos[0].source = other  # another recording is at that path now (other length): not used
    (tmp_path / "root" / "S" / "video-1.mp4").write_bytes(open(old, "rb").read())
    refresh_video_facts(state)
    assert state.videos[0].recorded == {}  # unknown for now: looked up again next time
    state.videos[0].source_type = "url"  # a stream is not downloaded again: unknown for good
    refresh_video_facts(state)
    assert state.videos[0].recorded == {"start": None, "from": "not stated in the file"}


def test_a_recording_start_entered_by_hand_is_used(tmp_path, chicago):
    from video_annote.media.recorded import EnteredTime
    from video_annote.sessions import SourceSpec, create_session, set_recording_start

    src = make_media(str(tmp_path / "cam.mp4"), duration=2, audio=None,
                     extra=("-metadata", "creation_time=2025-01-02T03:04:05Z"))
    root = str(tmp_path / "root")
    state = create_session(root, "S", [SourceSpec("video-1", src, "local", EnteredTime("1774024635.203"))]).state
    assert state.videos[0].recorded == {"start": "2026-03-20T11:37:15.203-05:00", "end": "2026-03-20T11:37:17.203-05:00",
                                        "start_unix": 1774024635.203, "end_unix": 1774024637.203,
                                        "from": "entered by hand (Unix time 1774024635.203)"}
    assert probe(str(tmp_path / "root" / "S" / "video-1.mp4")).raw["format"]["tags"]["creation_time"].startswith(
        "2026-03-20T16:37:15")  # the working copy states it too, to the second
    set_recording_start(state, "video-1", EnteredTime("1774024700000"))  # later, in milliseconds
    assert state.videos[0].recorded["start_unix"] == 1774024700
    set_recording_start(state, "video-1", EnteredTime("20261026152732", "year-first", "America/New_York"))
    assert state.videos[0].recorded == {  # an NVR's file name time, its clock on US Eastern time
        "start": "2026-10-26T14:27:32-05:00", "end": "2026-10-26T14:27:34-05:00",
        "start_unix": 1793042852, "end_unix": 1793042854, "from": "entered by hand (20261026152732, US Eastern time)"}
    set_recording_start(state, "video-1", EnteredTime(""))  # back to the time the file states
    assert state.videos[0].recorded["from"] == "the file's creation time, to the second"
    with pytest.raises(ValueError):
        create_session(root, "S2", [SourceSpec("video-1", src, "local", EnteredTime("yesterday"))])


def test_session_json_holds_what_people_read_and_what_the_app_needs(tmp_path):
    import json

    from video_annote import __version__
    from video_annote.core.models import AUDIO_KEYS
    from video_annote.sessions import SourceSpec, create_session

    ff = find_toolchain().ffmpeg
    src = str(tmp_path / "two_tracks.mp4")  # two audio tracks: worth a plain note
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=2", "-f", "lavfi", "-i",
                    "sine=d=2", "-f", "lavfi", "-i", "sine=f=500:d=2", "-map", "0", "-map", "1", "-map", "2",
                    "-c:v", "libx264", "-c:a", "aac", src], check=True)
    result = create_session(str(tmp_path / "root"), "S", [SourceSpec("video-1", src, "local")])
    assert result.notes == ["video-1: The recording has 2 audio tracks; only the main one was kept."]
    with open(tmp_path / "root" / "S" / "session.json", encoding="utf-8") as f:
        video = json.load(f)["videos"][0]
    assert list(video)[:5] == ["video_id", "filename", "source", "source_type", "recorded"]
    assert video["notes"] == ["The recording has 2 audio tracks; only the main one was kept."]
    assert video["imported"].endswith(f"with video-annote {__version__}") and "ingest" not in video
    assert set(video["media"]) == {"video_start_s", "audio"} and set(video["media"]["audio"]) <= set(AUDIO_KEYS)


def test_the_export_manifest_dates_every_clip(sources, tmp_path):
    import csv
    import json

    state = _single_video_session(tmp_path, sources["mp3_dualmono_48k_25fps.mp4"])
    state.videos[0].recorded = {"start": "2026-03-20T13:21:23.203735-05:00", "start_unix": 1774030883.203735,
                                "from": "the file's creation time, to the microsecond"}
    state.annotations = [_rec(1.0, 2.0)]
    report = export_segments(state, ExportOptions(out_dir=str(tmp_path / "out")))
    with open(report.manifest_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:  # frames 25..50 at 25 fps: 1.0 s to 2.04 s
        assert float(r["clip_start_unix"]) == pytest.approx(1774030884.203735, abs=1e-6)
        assert float(r["clip_end_unix"]) == pytest.approx(1774030885.243735, abs=1e-6)
    with open(report.manifest_json, encoding="utf-8") as f:
        assert json.load(f)["videos"]["video-1"]["recorded"]["start_unix"] == 1774030883.203735


def test_an_export_that_leaves_outputs_out_says_so_first(sources, tmp_path):
    from video_annote.media.export import left_out_outputs

    state = _single_video_session(tmp_path, sources["mp3_dualmono_48k_25fps.mp4"])
    state.annotations = [_rec(1.0, 2.0), _rec(2.0, 3.0)]
    options = ExportOptions(out_dir=str(tmp_path / "out"))
    assert left_out_outputs(state, options) == {}  # nothing exported yet
    export_segments(state, options)
    assert left_out_outputs(state, options) == {}
    assert left_out_outputs(state, dataclasses.replace(options, write_video=False)) == {"video": 2}
    assert left_out_outputs(state, dataclasses.replace(options, write_wav=False)) == {"wav": 2}
    state.annotations = state.annotations[:1]  # a deleted label's files are expected to go
    assert left_out_outputs(state, options) == {}


# ---------------- screen recordings (Apple AAC) ----------------

def test_aac_whose_priming_a_copy_would_play_is_stored_losslessly(tmp_path):
    from media_factory import apple_priming
    from video_annote.media.ingest import _lost_priming

    # as macOS screen recordings state it: audio after an empty edit, skipping 1024 samples of warm-up
    src = make_media(str(tmp_path / "screen.mp4"), duration=3, audio=AAC, audio_delay=0.05)
    apple_priming(src, 50, 1024)
    assert _lost_priming(src, probe(src), 30) == pytest.approx(1024 / 48000, abs=1e-6)
    result = ingest(src, str(tmp_path / "video-1.mp4"))
    assert result.ingest["audio"] == "lossless" and any("21 ms warm-up" in w for w in result.warnings)
    assert probe(str(tmp_path / "video-1.mp4")).audio.start_time == pytest.approx(0.05, abs=1e-6)
    assert difference_peak_db(src, str(tmp_path / "video-1.mp4"), timeline=False) < -120.0  # the same samples
    assert difference_peak_db(src, str(tmp_path / "video-1.mp4")) < -120.0  # at the same times

    plain = make_media(str(tmp_path / "plain.mp4"), duration=2, audio=AAC, audio_delay=0.05)  # as ffmpeg writes it
    assert _lost_priming(plain, probe(plain), 30) == 0 and ingest(plain, str(tmp_path / "v2.mp4")).ingest["audio"] == "copy"


@pytest.mark.parametrize("audio,action", [(MP3, "lossless"), (AAC, "copy")])
def test_audio_that_starts_between_milliseconds_keeps_its_exact_start(tmp_path, audio, action):
    # audio starting 4223 samples (87.979 ms) in, as on a macOS screen recording: edit lists in
    # milliseconds started it 0.979 ms early (-6.5 and -0.8 dB here instead of identical)
    src = make_media(str(tmp_path / "late.mp4"), duration=2, audio=audio, audio_delay=4223 / 48000,
                     extra=("-movie_timescale", "48000"))
    result = ingest(src, str(tmp_path / "video-1.mp4"))
    assert result.ingest["audio"] == action
    assert difference_peak_db(src, str(tmp_path / "video-1.mp4")) < -120.0


def test_an_hls_stream_imported_earlier_gets_its_program_date_time(tmp_path):
    from video_annote.media.recorded import find_recording_time
    from video_annote.sessions import SourceSpec, create_session, refresh_video_facts

    ff = find_toolchain().ffmpeg
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=4", "-c:v", "libx264", "-g", "25",
                    "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", str(tmp_path / "index.m3u8")], check=True)
    playlist = (tmp_path / "index.m3u8").read_text(encoding="utf-8")
    (tmp_path / "index.m3u8").write_text(playlist.replace("#EXTINF:", "#EXT-X-PROGRAM-DATE-TIME:2026-04-03T14:19:26.250Z\n"
                                                          "#EXTINF:", 1), encoding="utf-8")
    state = create_session(str(tmp_path / "root"), "S", [SourceSpec("video-1", str(tmp_path / "index.m3u8"), "local")]).state
    expected = state.videos[0].recorded
    assert expected["from"] == "the stream's program date-time, to the millisecond"
    state.videos[0].recorded = {}  # as stored before this version
    refresh_video_facts(state)
    assert state.videos[0].recorded == expected

    stamp = find_recording_time("x.m3u8", _fake_probe(), "2026-04-03T09:19:26.250")  # no zone, against the spec
    assert stamp is not None and stamp.assumed_zone and stamp.precision_s == 0.001


def test_recording_times_saved_by_a_preview_are_upgraded(chicago):
    from video_annote.media.recorded import upgrade_block

    assert upgrade_block({"start_utc": None, "end_utc": None, "precision_s": None, "source": None, "stated": {}}) == {}
    assert upgrade_block({"start_utc": "2026-03-20T16:37:15Z", "end_utc": "2026-03-20T16:50:01.8Z", "precision_s": 1.0,
                          "source": "the container's creation_time", "stated": {}}) == {
        "start": "2026-03-20T11:37:15-05:00", "end": "2026-03-20T11:50:01-05:00", "start_unix": 1774024635,
        "end_unix": 1774025401.8, "from": "the file's creation time, to the second"}
    today = {"start": None, "from": "not stated in the file"}
    assert upgrade_block(today) is today
