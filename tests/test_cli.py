"""Command-line exit codes: 0 success, 1 ran but found problems, 2 could not start."""
from __future__ import annotations

import pytest

from video_annote.cli import main


@pytest.fixture()
def root(tmp_path):
    (tmp_path / "S1").mkdir()
    (tmp_path / "S1" / "label.tsv").write_text("label\tstep_no\n", encoding="utf-8")
    return tmp_path


def test_arguments_that_cannot_work_exit_with_2(root, tmp_path):
    assert main(["validate", "--root", str(tmp_path / "missing")]) == 2
    assert main(["validate", "--root", str(root), "--session", "S1x", "--fast"]) == 2  # a typo
    assert main(["export", "--root", str(root), "--session", "S1x"]) == 2
    assert main(["import", "--root", str(tmp_path / "missing" / "root"), "--session", "S2", "a.mp4"]) == 2
    assert not (tmp_path / "missing").exists()  # a mistyped root is not created
    assert main(["import", "--root", str(root), "--session", ".hidden", "a.mp4"]) == 2
    assert main(["import", "--root", str(root), "--session", "S1", "a.mp4"]) == 2  # exists, no --replace
    assert main(["export", "--root", str(root), "--session", "S1", "--no-video", "--no-wav"]) == 2


def test_problems_found_exit_with_1(root):
    assert main(["validate", "--root", str(root), "--fast"]) == 1  # S1 has no videos: a problem found
    assert main(["export", "--root", str(root), "--session", "S1"]) == 2  # ... and nothing to export from


def test_probe_of_a_missing_file_exits_with_2(tmp_path):
    assert main(["probe", str(tmp_path / "missing.mp4")]) == 2


@pytest.mark.media
def test_an_export_that_writes_nothing_exits_with_1(tmp_path):
    from media_factory import make_media
    from video_annote.core.models import AnnotationRecord
    from video_annote.core.storage import save_annotations_tsv

    src = make_media(str(tmp_path / "cam.mp4"), duration=2, audio=None)
    root = tmp_path / "root"
    root.mkdir()
    assert main(["import", "--root", str(root), "--session", "S1", src]) == 0
    save_annotations_tsv(str(root / "S1"), [AnnotationRecord("S1", "video-1", 1, "x", 0, 25, 25, 0.0, 1.0, 1.0,
                                                             "video-1", "video-1")])
    # only WAVs requested, but the camera has no audio: nothing is written
    assert main(["export", "--root", str(root), "--session", "S1", "--no-video"]) == 1


@pytest.mark.media
def test_probe_shows_the_recording_time(tmp_path, capsys):
    from media_factory import make_media

    src = make_media(str(tmp_path / "cam.mp4"), duration=1, audio=None,
                     extra=("-metadata", "creation_time=2025-01-02T03:04:05Z"))
    assert main(["probe", src]) == 0
    assert "(Unix 1735787045 to 1735787046; from the file's creation time, to the second)" in capsys.readouterr().out


@pytest.mark.media
def test_import_takes_recording_starts_in_the_format_they_are_written(tmp_path, capsys):
    import json

    from media_factory import make_media

    src = make_media(str(tmp_path / "cam.mp4"), duration=1, audio=None)
    root = tmp_path / "root"
    root.mkdir()

    def imported(session, *start):
        assert main(["import", "--root", str(root), "--session", session, *start, src]) == 0
        with open(root / session / "session.json", encoding="utf-8") as f:
            return json.load(f)["videos"][0]["recorded"]

    assert main(["import", "--root", str(root), "--session", "S1", "--start", "2=1774024635", src]) == 2  # no video-2
    assert main(["import", "--root", str(root), "--session", "S1", "--start", "1=soon", src]) == 2
    assert main(["import", "--root", str(root), "--session", "S1", "--start", "1=20261026152732",  # (not Unix time)
                 "--start-format", "year-first", "--time-zone", "Mars/Olympus", src]) == 2
    recorded = imported("S1", "--start", "1=1774024635203")
    assert recorded["start_unix"] == 1774024635.203 and recorded["from"].startswith("entered by hand")
    recorded = imported("S2", "--start", "1=20261026152732", "--start-format", "year-first",
                        "--time-zone", "America/New_York")
    assert recorded["start_unix"] == 1793042852 and recorded["from"] == "entered by hand (20261026152732, US Eastern time)"
