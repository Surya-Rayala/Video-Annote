"""Regression checks on real recordings (opt-in: set VIDEO_ANNOTE_SAMPLES to a folder of videos).

For each recording the working copy must keep every video frame (apart from start-up frames that
share one frame slot, which are merged by design) and carry the source audio unchanged to 24-bit
precision on the same timeline (no second lossy generation, no offset), apart from decoded peaks
above full scale, which a lossless integer copy limits to full scale."""
from __future__ import annotations

import glob
import os

import pytest

from media_factory import audio_duration_on_timeline, audio_timestamp_gaps, difference_peak_db
from video_annote.media.ingest import _cadence, _first_frame_times, _merged_at_start, ingest, plan_ingest
from video_annote.media.probe import probe

pytestmark = pytest.mark.samples

FOLDER = os.environ.get("VIDEO_ANNOTE_SAMPLES", "")
FILES = sorted(p for ext in ("mp4", "mov", "mkv") for p in glob.glob(os.path.join(FOLDER, "**", f"*.{ext}"),
                                                                      recursive=True)) if FOLDER else []


@pytest.mark.parametrize("src", FILES or [""], ids=[os.path.relpath(p, FOLDER) for p in FILES] or ["none"])
def test_real_recording_imports_losslessly_and_in_sync(src, tmp_path):
    if not src:
        pytest.skip("no recordings found")
    info = probe(src)
    out = str(tmp_path / "video-1.mp4")
    result = ingest(src, out)
    copy = probe(out)
    assert copy.video is not None and not copy.video.is_variable_rate
    if info.video and info.video.frame_count and not info.video.is_variable_rate:
        exact = info.video.r_frame_rate == info.video.avg_frame_rate  # else timing jitter: +-1 frame
        times = _first_frame_times(src, info, 120)  # as the import counts the merged start-up frames
        merged = _merged_at_start(times, info, plan_ingest(info, cadence=_cadence(times)).frame_rate)
        assert abs(copy.video.frame_count - (info.video.frame_count - merged)) <= (0 if exact else 1)
    if info.audio is None:
        assert copy.audio is None
        return
    assert result.ingest["audio"] in ("copy", "lossless")
    # identical within 24-bit precision, sample after sample; decoded peaks above full scale (some MP3
    # recordings have them) are limited to full scale, as documented
    lossless = result.ingest["audio"] == "lossless"
    assert difference_peak_db(src, out, clip_a=lossless, timeline=False) < -120.0
    if audio_timestamp_gaps(src):
        # a recording with holes in its audio: the copy keeps the timeline's length (no drift), but the
        # holes cannot sit exactly where they were inside the re-framed audio
        assert abs(audio_duration_on_timeline(out) - audio_duration_on_timeline(src)) < 0.001
    else:
        assert difference_peak_db(src, out, clip_a=lossless) < -120.0  # and sample-aligned on the timeline
