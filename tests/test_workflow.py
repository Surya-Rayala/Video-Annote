"""The labeling workflow through the real MainWindow (headless and silent; macOS AVFoundation).

Covers the defects that corrupted labels in 0.1.x: stale capture times, the jump back to 00:00 at
the end of the media, rejected table edits that still changed the record, and timeline edits that
landed on the wrong record after a delete."""
from __future__ import annotations

import dataclasses
import os
import sys

import pytest

pytestmark = [pytest.mark.media, pytest.mark.gui,
              pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation backend only")]


@pytest.fixture()
def root(tmp_path):
    from media_factory import make_media
    from video_annote.sessions import SourceSpec, create_session

    a = make_media(str(tmp_path / "a.mp4"), duration=6, fps="25", audio=None)
    b = make_media(str(tmp_path / "b.mp4"), duration=6, fps="30", audio=None)
    root = tmp_path / "root"
    root.mkdir()
    create_session(str(root), "S1", [SourceSpec("video-1", a, "local"), SourceSpec("video-2", b, "local")])
    return str(root)


def test_label_capture_edit_and_delete(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import SkillStep
        from video_annote.core.storage import load_annotations_tsv, load_session_state

        monkeypatch.setattr(mw, "ask_confidence_and_notes", lambda *a, **k: (7, "ok"))
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Ok))

        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves"), SkillStep(4, "Check ID")])
        w.state, _ = load_session_state(root, "S1")
        w._load_session_into_ui()
        sdir = w.state.session_dir
        grid = w.video_grid
        rows = lambda: load_annotations_tsv(sdir)[0]  # noqa: E731
        r = {}

        def select(number):
            for i in range(w.skills_panel.list.count()):
                if w.skills_panel.list.item(i).data(0x0100).number == number:  # Qt.UserRole
                    w.skills_panel.list.setCurrentRow(i)

        run_steps(app, [
            (1500, lambda: r.update(ready=grid.is_ready())),
            # label 1: start/end placed mid-frame while paused
            (10, lambda: select(3)),
            (10, w._start_step),
            (10, lambda: grid.seek(1013)),
            (400, w._confirm_start),
            (10, lambda: grid.seek(2519)),
            (400, w._end_step),
            (10, w._finish_step),
            (100, lambda: r.update(first=rows())),
            # label 2: Start/Confirm Start while playing, End after the media has ended
            (10, lambda: select(4)),
            (10, lambda: grid.seek(4000)),
            (300, grid.play),
            (400, w._start_step),  # pauses
            (10, lambda: r.update(start_pos=grid.position())),
            (10, w._confirm_start),  # resumes playback (it was playing)
            (2600, lambda: r.update(ended=grid.at_end())),
            (10, w._end_step),
            (300, w._finish_step),
            (100, lambda: r.update(second=rows())),
            # a rejected-style edit is validated on a copy; a valid one is snapped to frames
            (10, lambda: w._apply_record_edit(w.state.annotations[0],
                                              dataclasses.replace(w.state.annotations[0], end_time=3.013))),
            (100, lambda: r.update(edited=rows()[0])),
            # timeline edit of row 2 survives deleting row 1 underneath it
            (10, lambda: w.skill_timeline.canvas._enter_edit_mode(w.state.annotations[1])),
            (10, lambda: w._delete_record(w.state.annotations[0])),
            (10, lambda: w.skill_timeline.exit_editing(commit=True)),
            (100, lambda: r.update(after_delete=rows())),
            (10, lambda: w._close_session(ask=False)),
        ])

    assert r["ready"]
    first = r["first"][0]
    # 1.013 s shows frame 25 (1.000-1.040) and 2.519 s shows frame 62 (2.480-2.520) at 25 fps
    assert (first.start_frame, first.end_frame, first.total_frames) == (25, 62, 37)
    assert (first.start_time, first.end_time) == (1.0, 2.48)
    assert (first.step_no, first.step_name, first.confidence, first.notes) == (3, "Wear Gloves", 7, "ok")
    assert first.camid == "video-1" and first.time_source == "video-1"

    second = r["second"][1]
    assert r["ended"]
    assert abs(second.start_time - r["start_pos"] / 1000) <= 0.04  # the frame on screen, not a stale value
    assert second.end_frame == 149 and second.end_time == pytest.approx(5.96)  # last frame, not 00:00

    assert (r["edited"].end_time, r["edited"].end_frame) == (3.0, 75)  # snapped to the frame start

    remaining = r["after_delete"]
    assert len(remaining) == 1 and remaining[0].step_no == 4
    assert remaining[0].end_frame == 149  # the edit committed onto the right record, unchanged


def test_end_capture_stays_on_the_last_video_frame_when_audio_runs_longer(tmp_path, monkeypatch):
    """Recordings often carry audio past the last video frame (0.3 s in the room cameras); the
    player's duration is that of the longest track, but labels must stop at the last frame."""
    import subprocess

    from qt_headless import headless_qt, run_steps
    from video_annote.media.ffmpeg import find_toolchain
    from video_annote.sessions import SourceSpec, create_session

    src = str(tmp_path / "long_audio.mp4")
    subprocess.run([find_toolchain().ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=25:d=3",
                    "-f", "lavfi", "-i", "sine=f=300:d=3.4", "-c:v", "libx264", "-c:a", "aac", src], check=True)
    root = tmp_path / "root"
    root.mkdir()
    create_session(str(root), "S1", [SourceSpec("video-1", src, "local")])

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import SkillStep
        from video_annote.core.storage import load_annotations_tsv, load_session_state

        monkeypatch.setattr(mw, "ask_confidence_and_notes", lambda *a, **k: (5, ""))
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        w = mw.MainWindow(root_dir=str(root))
        w.skills_panel.set_skills([SkillStep(1, "Step")])
        w.state, _ = load_session_state(str(root), "S1")
        w._load_session_into_ui()
        grid = w.video_grid
        r = {}
        run_steps(app, [
            (3000, lambda: w.skills_panel.list.setCurrentRow(0)),  # (warm-up: slower on a busy machine)
            (10, w._start_step),
            (10, lambda: grid.seek(2000)),
            (300, w._confirm_start),
            (10, grid.play),
            (2500, lambda: r.update(duration=grid.duration(), ended=grid.at_end())),
            (10, w._end_step),
            (300, w._finish_step),
            (100, lambda: r.update(rows=load_annotations_tsv(w.state.session_dir)[0])),
            (10, lambda: w._close_session(ask=False)),
        ])

    assert r["ended"] and r["duration"] == 3000  # the video track, not the 3.4 s audio
    assert r["rows"][0].end_frame == 74 and r["rows"][0].end_time == pytest.approx(2.96)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can read any file")
def test_unreadable_config_is_never_overwritten(tmp_path, monkeypatch):
    from qt_headless import headless_qt

    original = '{"skills": [{"number": 1, "name": "Gloves"}]}'
    cfg = tmp_path / "config.json"
    cfg.write_text(original, encoding="utf-8")
    os.chmod(cfg, 0)  # e.g. a 0600 file written by 0.1.x for another user: cannot be read or copied
    try:
        with headless_qt():
            from PyQt5.QtWidgets import QMessageBox

            import video_annote.ui.main_window as mw
            from video_annote.core.models import SkillStep

            shown = []
            monkeypatch.setattr(QMessageBox, "warning",
                                staticmethod(lambda *a, **k: shown.append(a[2]) or QMessageBox.Ok))
            w = mw.MainWindow(root_dir=str(tmp_path))
            w.skills_panel.set_skills([SkillStep(2, "Other")])
            w._on_skills_changed()
            w.close()
    finally:
        os.chmod(cfg, 0o644)
    assert cfg.read_text(encoding="utf-8") == original
    assert shown and "No copy could be made" in shown[0]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can read any file")
def test_selecting_the_root_again_reloads_a_fixed_config(tmp_path, monkeypatch):
    from qt_headless import headless_qt

    cfg = tmp_path / "config.json"
    cfg.write_text('{"skills": [{"number": 1, "name": "Gloves"}]}', encoding="utf-8")
    os.chmod(cfg, 0)
    try:
        with headless_qt():
            from PyQt5.QtWidgets import QFileDialog, QMessageBox

            import video_annote.ui.main_window as mw

            monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Ok))
            monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(tmp_path)))
            w = mw.MainWindow(root_dir=str(tmp_path))
            assert w._cfg_frozen and w.skills_panel.skills() == []
            os.chmod(cfg, 0o644)  # the user fixes the file, then selects the same root again
            w._choose_root_dir()
            assert not w._cfg_frozen and [s.name for s in w.skills_panel.skills()] == ["Gloves"]
            w.close()
    finally:
        os.chmod(cfg, 0o644)


def test_session_folder_moved_while_open_is_never_written_into(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import AnnotationRecord
        from video_annote.core.storage import load_session_state

        shown = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[1]) or QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
        w = mw.MainWindow(root_dir=root)
        w.state, _ = load_session_state(root, "S1")
        w._load_session_into_ui()
        r = {}

        def move_and_edit():
            os.rename(os.path.join(root, "S1"), os.path.join(root, "S1-moved"))
            w.state.annotations.append(AnnotationRecord("S1", "video-1", 3, "x", 25, 50, 25, 1.0, 2.0, 1.0,
                                                        "video-1", "video-1", 5, "after the move"))
            r["saved"] = w._save()
            r["recovery"] = w._recovery_dir
            r["closed"] = w._close_session(ask=False)
            r["note"] = w._close_note

        run_steps(app, [(1500, move_and_edit)])

    assert r["saved"] and r["closed"] and "Session folder replaced or moved" in shown
    assert not os.path.exists(os.path.join(root, "S1"))  # the old path was not recreated
    assert "after the move" in open(os.path.join(r["recovery"], "label.tsv"), encoding="utf-8").read()
    assert r["recovery"] in r["note"]


def test_finish_session_says_when_changes_were_not_saved(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.storage import load_session_state

        told = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: told.append(a[2])))
        w = mw.MainWindow(root_dir=root)
        w.state, _ = load_session_state(root, "S1")
        w._load_session_into_ui()

        def failing(_state):
            raise OSError("No space left on device")

        def finish():
            monkeypatch.setattr(mw, "persist_session_state", failing)
            w._finish_session()

        run_steps(app, [(1500, finish)])

    assert told and "without saving" in told[0] and "saved and closed" not in told[0]


def test_edits_and_capture_controls(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import AnnotationRecord, SkillStep
        from video_annote.core.storage import load_session_state
        from video_annote.core.timecode import recompute_from_times

        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[1]) or QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: warnings.append(a[1]) or QMessageBox.Ok))
        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        w.state, _ = load_session_state(root, "S1")
        w._load_session_into_ui()

        def label(ts):
            return recompute_from_times(AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 0, 0, 0, 1.0, 2.0, 1.0,
                                                         ts, "video-1"), 30)

        r = {}

        def add_labels():
            w.state.annotations = [label("video-2"), label("video-9"), label("VIDEO-2")]
            w._refresh_annotations_view()

        def drag_and_edit():
            a = w.state.annotations
            # the end handle dragged to 2033 ms showed frame 60 (2000-2033.3 ms): no slack for a drag
            w._on_timeline_edit_committed(a[0], 1000, 2033)
            w._apply_record_edit(a[1], dataclasses.replace(a[1], end_time=2.5))  # unknown time source
            w._apply_record_edit(a[2], dataclasses.replace(a[2], end_time=2.5))  # same video, other case
            r["edits"] = [(x.end_time, x.end_frame) for x in w.state.annotations]

        def capture_controls():
            w.skills_panel.list.setCurrentRow(0)
            w._start_step()
            r["cancel_enabled"] = w.btn_cancel_step.isEnabled()
            w._on_time_source_changed("video-1")  # allowed until Confirm Start
            r["ts_before_confirm"] = w.video_grid.time_source()
            w._confirm_start()
            w._on_time_source_changed("video-2")  # locked now: refused
            r["ts_after_confirm"] = w.video_grid.time_source()
            w._cancel_step()
            r["after_cancel"] = (w.state.is_capturing, w.btn_cancel_step.isEnabled(), len(w.state.annotations))

        def drag_with_other_time_source():
            # the label is on video-2 (30 fps); the grid shows video-1 (25 fps): at 2039 ms it shows
            # video-1 frame 50 and video-2 frame 60 (the frame mapped from 2.000 s), so the edge is 60
            w._on_timeline_edit_committed(w.state.annotations[2], 1000, 2039)
            r["other_ts"] = (w.state.annotations[2].end_time, w.state.annotations[2].end_frame)

        run_steps(app, [
            (1500, lambda: w._on_time_source_changed("video-2")),
            (300, add_labels),
            (10, drag_and_edit),
            (10, capture_controls),
            (10, drag_with_other_time_source),
            (100, lambda: w._close_session(ask=False)),
        ])

    assert r["edits"] == [(2.0, 60), (2.0, 60), (2.5, 75)]
    assert r["other_ts"] == (2.0, 60)
    assert any("Invalid edit" in t for t in warnings)
    assert r["cancel_enabled"] and r["ts_before_confirm"] == "video-1" and r["ts_after_confirm"] == "video-1"
    assert r["after_cancel"] == (False, False, 3)


def test_round3_edit_and_safety_rules(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    root_parent, root_name = os.path.split(root)
    monkeypatch.chdir(root_parent)
    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import AnnotationRecord, SkillStep
        from video_annote.core.storage import load_session_state

        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[1]) or QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
        dialogs = []
        monkeypatch.setattr(mw.ExportDialog, "exec_", lambda self: dialogs.append(1) or 0)
        w = mw.MainWindow(root_dir=root_name)  # relative, as `video-annote gui --root root` passes it
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        w.state, _ = load_session_state(w.root_dir, "S1")
        w._load_session_into_ui()
        r = {}

        def legacy_edit():
            r["failed"] = w.video_grid.failed_videos()
            w._on_time_source_changed("video-2")  # 30 fps
            # a label made by 0.1.x: 1.25 s lies between frames 37 and 38; label.tsv numbers it 38
            old = AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 38, 60, 22, 1.25, 2.0, 0.75,
                                   "video-2", "video-1", 5, "")
            w.state.annotations = [old]
            w._apply_record_edit(old, dataclasses.replace(old, end_time=2.5))  # only the end is edited
            r["legacy"] = (w.state.annotations[0].start_frame, w.state.annotations[0].end_frame)

        def drag_onto_start():
            rec = w.state.annotations[0]  # frames 38..75
            w._on_timeline_edit_committed(rec, int(round(rec.start_time * 1000)), int(round(rec.start_time * 1000)))
            r["dragged"] = (w.state.annotations[0].start_frame, w.state.annotations[0].end_frame)

        def unknown_rate():
            item = w.state.get_video("video-2")
            item.frame_rate, item.fps = "", 0.0  # as without ffprobe for an inferred video
            rec = w.state.annotations[0]
            w._apply_record_edit(rec, dataclasses.replace(rec, end_time=3.0))
            r["edit_refused"] = w.state.annotations[0].end_time == rec.end_time
            w._on_time_source_changed("video-1")
            w.skills_panel.list.setCurrentRow(0)
            w._start_step()
            w._on_time_source_changed("video-2")  # allowed before Confirm Start ...
            w._confirm_start()  # ... but recording with it is not
            r["confirm_refused"] = w.state.pending_start_ms is None
            w._cancel_step()

        def moved_then_export():
            os.rename(os.path.join(w.root_dir, "S1"), os.path.join(w.root_dir, "S1-moved"))
            w._export_clips()
            r["export_dialogs"] = len(dialogs)

        run_steps(app, [(1500, legacy_edit), (100, drag_onto_start), (100, unknown_rate), (100, moved_then_export)])

    assert r["failed"] == []  # a relative root still opens the videos
    assert r["legacy"] == (38, 75)  # the untouched start keeps label.tsv's frame (displayed_frame gives 37)
    assert r["dragged"] == (38, 38)  # the end handle stops at the start, which does not move
    assert r["edit_refused"] and r["confirm_refused"] and "Frame rate unknown" in warnings
    assert r["export_dialogs"] == 0 and "Cannot export" in warnings


def test_round4_rules(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import AnnotationRecord, SkillStep
        from video_annote.core.storage import load_annotations_tsv, save_annotations_tsv
        from video_annote.media.export import ExportOptions
        from video_annote.media.ffmpeg import FFmpegNotFoundError

        warnings, infos = [], []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[1]) or QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
        monkeypatch.setattr(mw.MainWindow, "_show_session_info",
                            lambda self, title="": infos.append(list(self._session_notes)))
        sdir = os.path.join(root, "S1")
        # a label.tsv copied from a session whose time source was stored as 60 fps: 2.0-4.0 s = frames 120..240
        save_annotations_tsv(sdir, [AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 120, 240, 120, 2.0, 4.0, 2.0,
                                                     "video-2", "video-1")])
        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        r = {}

        def open_copied():
            w._open_session("S1")
            rec = w.state.annotations[0]
            r["recomputed"] = (rec.start_frame, rec.end_frame, rec.total_frames, rec.start_time)
            r["note"] = any("recomputed" in n for n in infos[-1])

        def legacy_one_frame_edits():
            w._on_time_source_changed("video-2")  # 30 fps
            # labels made by 0.1.x: 1.25 s lies between frames 37 and 38; label.tsv numbers it 38
            dragged = AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 38, 60, 22, 1.25, 2.0, 0.75, "video-2",
                                       "video-1", 5, "")
            typed = dataclasses.replace(dragged)
            w.state.annotations = [dragged, typed]
            w._refresh_annotations_view()
            w._on_timeline_edit_committed(dragged, 1250, 1250)  # the end handle dragged onto the start
            w._apply_record_edit(typed, dataclasses.replace(typed, end_time=typed.start_time))  # typed in the table
            r["one_frame"] = [(x.start_frame, x.end_frame) for x in w.state.annotations]

        def empty_export():
            class Dialog:
                Accepted = 1

                def __init__(self, *args):
                    pass

                def exec_(self):
                    return 1

                def options(self):  # WAVs only, but the audio source has no audio
                    return ExportOptions(out_dir=os.path.join(root, "exports"), write_video=False)

            monkeypatch.setattr(mw, "ExportDialog", Dialog)
            w._export_clips()

        def unknown_rate_survives_a_save():
            item = w.state.get_video("video-2")
            item.frame_rate, item.fps = "", 0.0  # never measured (inferred without ffprobe)
            w._close_session(ask=False)

            def no_ffprobe(state):
                raise FFmpegNotFoundError("ffmpeg and ffprobe are required but were not found.")

            monkeypatch.setattr(mw, "refresh_video_facts", no_ffprobe)
            w._open_session("S1")
            r["still_unknown"] = not w.state.get_video("video-2").rate_known
            r["unknown_note"] = any("is unknown (ffmpeg/ffprobe is needed" in n for n in infos[-1])

        def start_with_unknown_rate():
            r["ready"] = w.video_grid.is_ready()
            w._on_time_source_changed("video-2")
            w.skills_panel.list.setCurrentRow(0)
            w._start_step()
            r["start_refused"] = w.state.pending_step is None
            r["tsv"] = [(x.start_frame, x.end_frame) for x in load_annotations_tsv(sdir)[0]]
            w._close_session(ask=False)

        run_steps(app, [(10, open_copied), (1500, legacy_one_frame_edits), (100, empty_export),
                        (100, unknown_rate_survives_a_save), (1500, start_with_unknown_rate)])

    assert r["recomputed"] == (60, 120, 60, 2.0) and r["note"]
    assert r["one_frame"] == [(38, 38), (38, 38)]  # not refused as "end before start"
    assert "Nothing exported" in warnings and "Export finished" not in warnings
    assert r["still_unknown"] and r["unknown_note"] and r["ready"] and r["start_refused"]
    assert "Frame rate unknown" in warnings
    assert r["tsv"] == [(38, 38), (38, 38)]


def test_round5_rules(root, monkeypatch, tmp_path):
    from qt_headless import headless_qt, run_steps

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # recovery copies may fall back to ~
    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import AnnotationRecord, SkillStep
        from video_annote.core.timecode import frame_at_ms, recompute_from_times

        shown = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[1]) or QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
        infos, errors = [], []
        monkeypatch.setattr(mw.MainWindow, "_show_session_info", lambda self, title="": infos.append(title))
        monkeypatch.setattr(mw, "show_error", lambda parent, title, *a, **k: errors.append(title))
        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        r = {}

        def mirror_drag():
            w._open_session("S1")
            w._on_time_source_changed("video-2")  # 30 fps
            # a 0.1.x label whose end (2.25 s) lies in the later half of frame 67: label.tsv numbers it 68
            legacy = AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 30, 68, 38, 1.0, 2.25, 1.25, "video-2",
                                      "video-1", 5, "")
            w.state.annotations = [legacy]
            w._refresh_annotations_view()
            w._on_timeline_edit_committed(legacy, 2250, 2250)  # the start handle dragged onto the end
            rec = w.state.annotations[0]
            r["mirror"] = (rec.start_frame, rec.end_frame)

        def unknown_rate_readout():
            item = w.state.get_video("video-2")
            item.frame_rate, item.fps = "", 0.0
            w._update_time_label(1500)
            r["readout"] = w.frame_label.text()
            item.frame_rate, item.fps = "30/1", 30.0

        def two_handle_edit():
            # a 0.2 label 31..64 (end 2.133 s): the end is dragged away first, then the start is dropped exactly
            # on the end's old millisecond, where frame 63 is shown: the start becomes 63, not the old end's 64
            rec = recompute_from_times(AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 0, 0, 0, 31 / 30, 64 / 30,
                                                        0, "video-2", "video-1", 5, ""), 30)
            w.state.annotations = [rec]
            w._refresh_annotations_view()
            w._on_timeline_edit_committed(rec, 2133, 3312)
            r["two_handles"] = (w.state.annotations[0].start_frame, w.state.annotations[0].end_frame)

        def preview_on_the_other_edge():
            rec = recompute_from_times(AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 0, 0, 0, 31 / 30, 67 / 30,
                                                        0, "video-2", "video-1", 5, ""), 30)  # end 2.233 s
            w.state.annotations = [rec]
            w._refresh_annotations_view()
            w._timeline_dragging = True
            w._on_timeline_edit_preview(rec, 2233, 2233, "left")  # the start handle rests on the end
            w._timeline_dragging = False

        def preview_shown():
            r["preview"] = frame_at_ms(w.video_grid.position(), 30)

        def unreadable_session_json():
            w._close_session(ask=False)
            meta = os.path.join(root, "S1", "session.json")
            before = open(meta, "rb").read()
            os.chmod(meta, 0)
            try:
                r["opened"] = w._open_session("S1")
            finally:
                os.chmod(meta, 0o644)
            r["meta_kept"] = open(meta, "rb").read() == before

        run_steps(app, [(10, mirror_drag), (1500, unknown_rate_readout), (10, two_handle_edit),
                        (10, preview_on_the_other_edge), (400, preview_shown), (10, unreadable_session_json)])

    assert r["mirror"] == (68, 68)  # one frame, on the end's label.tsv frame (not 67..68)
    assert r["readout"].endswith("frame rate unknown")
    assert r["two_handles"] == (63, 99)
    assert r["preview"] == 67  # the frame the label would get, not 66
    assert r["opened"] is False and r["meta_kept"] and "Cannot open session" in errors


# ---------------- ease of use (0.2) ----------------

def test_keyboard_labeling_undo_and_frame_exact_corrections(root, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import SkillStep
        from video_annote.core.storage import load_annotations_tsv
        from video_annote.media.recorded import local_text
        from video_annote.ui.common import app_settings

        monkeypatch.setattr(mw, "ask_confidence_and_notes", lambda *a, **k: (9, ""))
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        grid = w.video_grid
        r = {}

        def frames():
            return [(a.start_frame, a.end_frame) for a in load_annotations_tsv(w.state.session_dir)[0]]

        def open_session():
            w._open_session("S1")
            r["remembered"] = (app_settings().value("last_root"), app_settings().value("last_session"))
            r["title"] = w.windowTitle()
            r["start_without_label"] = w.btn_start_step.isEnabled()

        def mark_start():
            w.skills_panel.list.setCurrentRow(0)
            r["start_with_label"] = w.btn_start_step.isEnabled()
            grid.seek(1013)  # frame 25 at 25 fps

        def key_in_twice():
            w._key_start()  # Start ...
            w._key_start()  # ... and Confirm Start: the frame on screen
            r["locked"] = not w.combo_time_source.isEnabled()
            grid.seek(2519)  # frame 62

        def key_out_twice():
            w._key_end()
            w._key_end()
            r["recorded"] = frames()
            w._undo_change()
            r["undone"] = frames()
            w._redo_change()
            r["redone"] = frames()

        def set_edges():
            rec = w.state.annotations[0]
            w.table.select_record(rec)
            grid.seek(1213)  # frame 30

        def set_start_here():
            w._set_edge_here(w.table.selected_record(), "start")
            r["start_set"] = frames()
            w._go_to_edge("end")

        def at_end():
            r["at_end"] = grid.frame_at(grid.position())
            w._on_timeline_record_clicked(w.state.annotations[0])  # a click shows the label's first frame

        def clicked():
            r["clicked"] = grid.frame_at(grid.position())
            w.state.videos[0].recorded = {"start": "2026-03-20T13:21:23.203735-05:00", "start_unix": 1774030883.203735,
                                          "from": "the file's creation time, to the microsecond"}
            w._update_time_label(grid.position())
            r["readout"] = w.frame_label.text()
            w._delete_selected()  # Delete key (asks first)
            r["deleted"] = frames()
            w._undo_change()
            r["restored"] = frames()
            w._key_start()
            w._key_escape()  # Esc cancels a label being recorded
            r["cancelled"] = not w.state.is_capturing

        run_steps(app, [(10, open_session), (1500, mark_start), (400, key_in_twice), (400, key_out_twice),
                        (100, set_edges), (400, set_start_here), (400, at_end), (400, clicked)])
        w._close_session(ask=False)

    assert r["remembered"] == (root, "S1") and r["title"].startswith("S1")
    assert not r["start_without_label"] and r["start_with_label"] and r["locked"]
    assert r["recorded"] == [(25, 62)] and r["undone"] == [] and r["redone"] == [(25, 62)]
    assert r["start_set"] == [(30, 62)]  # exactly the frame on screen
    assert r["at_end"] == 62 and r["clicked"] == 30
    shown = datetime(2026, 3, 20, 18, 21, 23, 203735, timezone.utc) + timedelta(seconds=30 / 25)
    assert r["readout"].endswith(local_text(shown, 1e-6, date=False))  # the real-world time of that frame
    assert r["deleted"] == [] and r["restored"] == [(30, 62)] and r["cancelled"]


def test_the_window_fits_a_laptop_screen_whatever_the_paths(root):
    from qt_headless import headless_qt

    with headless_qt() as app:
        import video_annote.ui.main_window as mw

        w = mw.MainWindow(root_dir=root)
        w.root_label.setText("/Users/someone/" + "very long folder name/" * 12)
        w.session_label.setText("S1  —  " + "/another/very/long/path" * 10)
        app.processEvents()
        assert w.minimumSizeHint().width() <= 1100
        w._save_layout()
        w.close()


def test_pickers_ignore_the_wheel_and_toggle_views_on_a_row_click(root):
    from qt_headless import headless_qt

    with headless_qt() as app:
        from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
        from PyQt5.QtGui import QMouseEvent, QWheelEvent
        from PyQt5.QtWidgets import QApplication

        import video_annote.ui.main_window as mw

        w = mw.MainWindow(root_dir=root)
        w._open_session("S1")
        combo = w.combo_time_source
        assert combo.itemText(0).startswith("video-1 · a.mp4")  # named after its recording
        wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(combo, wheel)
        assert combo.currentData() == "video-1" and w.state.time_source_id == "video-1"

        views = w.combo_views
        views.showPopup()
        app.processEvents()
        view = views.view()
        rect = view.visualRect(views.model().index(1, 0))
        for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            QApplication.sendEvent(view.viewport(), QMouseEvent(kind, QPointF(rect.center()), Qt.LeftButton,
                                                                Qt.LeftButton, Qt.NoModifier))
        app.processEvents()
        assert views.checked_ids() == ["video-1", "video-2"] and w.video_grid.active_views() == ["video-1", "video-2"]
        views.hidePopup()
        w._close_session(ask=False)


def test_dialogs_guide_instead_of_complaining(tmp_path):
    from qt_headless import headless_qt

    from media_factory import make_media

    src = make_media(str(tmp_path / "cam.mp4"), duration=1, audio=None)
    with headless_qt():
        from PyQt5.QtWidgets import QDialogButtonBox

        from video_annote.ui.dialogs.create_session import CreateSessionDialog
        from video_annote.ui.dialogs.export_dialog import ExportDialog

        dlg = CreateSessionDialog()
        ok = dlg.buttons.button(QDialogButtonBox.Ok)
        assert not ok.isEnabled()  # no label, no videos
        dlg.label_edit.setText("S2")
        dlg._add_paths([src, src])
        assert ok.isEnabled() and ok.text() == "Import"
        dlg.list.setCurrentRow(1)
        dlg._move(-1)
        assert dlg.list.currentRow() == 0 and len(dlg.sources()) == 2

        first = ExportDialog(str(tmp_path / "exports"), 3)
        first.write_video.setChecked(False)
        first.cameras.setCurrentIndex(2)
        first._save_choices()
        again = ExportDialog(str(tmp_path / "exports"), 3)
        assert not again.write_video.isChecked() and again.cameras.currentIndex() == 2  # offered again
        again.write_video.setChecked(True)
        again._save_choices()


def test_a_click_on_the_progress_bar_goes_there_except_while_recording(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtCore import QEvent, QPointF, Qt
        from PyQt5.QtGui import QMouseEvent
        from PyQt5.QtWidgets import QApplication

        import video_annote.ui.main_window as mw
        from video_annote.core.models import SkillStep

        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        w.resize(1200, 800)
        w.show()
        r = {}

        def click(fraction):
            slider = w.slider
            x = int(slider.width() * fraction)
            event = QMouseEvent(QEvent.MouseButtonPress, QPointF(x, slider.height() / 2), Qt.LeftButton,
                                Qt.LeftButton, Qt.NoModifier)
            QApplication.sendEvent(slider, event)

        def idle_click():
            w._open_session("S1")

        def go():
            click(0.75)

        def check_and_record():
            r["idle"] = w.video_grid.position()
            w.skills_panel.list.setCurrentRow(0)
            w._start_step()
            click(0.25)

        def check():
            r["recording"] = w.video_grid.position()
            w._cancel_step()

        run_steps(app, [(10, idle_click), (1500, go), (400, check_and_record), (400, check)])
        w._close_session(ask=False)

    assert 4000 <= r["idle"] <= 5000  # three quarters of 6 s
    assert r["recording"] == r["idle"]  # not moved while a label is being recorded


def test_single_keys_label_from_the_list_but_type_into_text_fields(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtCore import Qt
        from PyQt5.QtTest import QTest
        from PyQt5.QtWidgets import QApplication, QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import SkillStep

        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Ok))
        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        w.show()
        r = {}

        def open_session():
            w._open_session("S1")
            QApplication.setActiveWindow(w)

        def type_in_name_field():
            w.skills_panel.list.setCurrentRow(0)
            field = w.skills_panel.name_input
            field.setFocus()
            for key in (Qt.Key_I, Qt.Key_O, Qt.Key_Space):
                QTest.keyClick(field, key)
            QTest.keyClick(field, Qt.Key_I, Qt.ShiftModifier)
            r["typed"] = (field.text(), w.state.is_capturing, w.video_grid.is_playing())
            field.clear()

        def keys_on_the_list():
            w.skills_panel.list.setFocus()
            QTest.keyClick(w.skills_panel.list, Qt.Key_I)
            r["started"] = w.state.pending_step is not None and w.state.pending_start_ms is None
            QTest.keyClick(w.skills_panel.list, Qt.Key_Escape)
            r["cancelled"] = not w.state.is_capturing

        run_steps(app, [(10, open_session), (1500, type_in_name_field), (100, keys_on_the_list)])
        w._close_session(ask=False)

    assert r["typed"] == ("io i", False, False)  # typed (QTest types Shift+I as "i"), not Start/End/play/set start
    assert r["started"] and r["cancelled"]


# ---------------- review of the 0.2 ease-of-use code ----------------

def test_shown_and_set_edges_are_the_frames_label_tsv_gives(root, monkeypatch):
    """Stored times have 3 decimals: frame 61 at 30 fps is 2.033 s, and 2033 ms lies in frame 60. Showing
    an edge must show 61, and setting it there must change nothing (no undo step either)."""
    from qt_headless import headless_qt, run_steps, shown_frame

    with headless_qt() as app:
        from PyQt5.QtWidgets import QMessageBox

        import video_annote.ui.main_window as mw
        from video_annote.core.models import AnnotationRecord, SkillStep
        from video_annote.core.storage import load_annotations_tsv
        from video_annote.core.timecode import recompute_from_times

        refused = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: refused.append(a[2]) or QMessageBox.Ok))
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        grid = w.video_grid
        r = {}

        def label(first, last):
            return recompute_from_times(AnnotationRecord("S1", "video-2", 3, "Wear Gloves", 0, 0, 0, first / 30,
                                                         last / 30, 0, "video-2", "video-1"), 30)

        def frames():
            return [(a.start_frame, a.end_frame) for a in load_annotations_tsv(w.state.session_dir)[0]]

        def on_screen():
            return grid.frame_at(grid.position()), shown_frame(grid._players["video-2"])

        def open_session():
            w._open_session("S1")

        def setup():
            w._on_time_source_changed("video-2")  # 30 fps
            w.state.annotations = [label(31, 61), label(2, 30)]
            w._refresh_annotations_view()
            w._save()
            w.table.select_record(w.state.annotations[0])
            w._go_to_edge("end")

        def at_end():
            r["end"] = on_screen()
            undo = len(w._undo)
            w._set_edge_here(w.table.selected_record(), "end")  # already there: no change, no undo step
            r["noop"] = (frames()[0], len(w._undo) - undo)
            w._go_to_edge("start")

        def at_start():
            r["start"] = on_screen()
            w._on_timeline_record_clicked(w.state.annotations[1])  # a click shows a label's first frame
            w.table.select_record(w.state.annotations[0])
            grid.seek(2040)  # frame 61: a one-frame label on the end's frame

        def one_frame():
            w._set_edge_here(w.table.selected_record(), "start")
            r["one_frame"] = frames()[0]
            # the selection outlives a change to another row, and an empty click clears it everywhere
            w._delete_record(w.state.annotations[1])
            r["kept"] = w.table.selected_record() is w.state.annotations[0]
            w._on_timeline_record_clicked(None)
            r["cleared"] = (w.table.selected_record(), w.skill_timeline.selected_record())

        run_steps(app, [(10, open_session), (1500, setup), (400, at_end), (400, at_start), (400, one_frame)])
        w._close_session(ask=False)

    assert r["end"] == (61, 61) and r["start"] == (31, 31)  # the decoded picture too
    assert r["noop"] == ((31, 61), 0)
    assert r["one_frame"] == (61, 61) and not refused
    assert r["kept"] and r["cleared"] == (None, None)


def test_return_and_esc_stay_with_the_text_field_being_typed_in(root, monkeypatch):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtCore import Qt
        from PyQt5.QtTest import QTest
        from PyQt5.QtWidgets import QApplication

        import video_annote.ui.main_window as mw
        from video_annote.core.models import SkillStep

        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        w.show()
        r = {}

        def setup():
            w._open_session("S1")
            QApplication.setActiveWindow(w)

        def typing_while_recording():
            w.skills_panel.list.setCurrentRow(0)
            w._start_step()
            w._confirm_start()
            field = w.skills_panel.name_input
            field.setFocus()
            QTest.keyClicks(field, "Gown")
            QTest.keyClick(field, Qt.Key_Escape)  # not a cancel of the label being recorded
            QTest.keyClick(field, Qt.Key_Return)  # adds the label
            r["recording"] = w.state.is_capturing
            r["labels"] = [s.name for s in w.skills_panel.skills()]
            w._cancel_step()

        def held_handle_cancelled():
            canvas = w.skill_timeline.canvas
            w._refresh_annotations_view()
            from video_annote.core.models import AnnotationRecord
            from video_annote.core.timecode import recompute_from_times
            rec = recompute_from_times(AnnotationRecord("S1", "video-1", 3, "Wear Gloves", 0, 0, 0, 1.0, 2.0, 0,
                                                        "video-1", "video-1"), 25)
            w.state.annotations = [rec]
            w._refresh_annotations_view()
            canvas._enter_edit_mode(rec)
            canvas._drag_edge = "right"  # a handle is pressed ...
            canvas.drag_started.emit()
            w.skills_panel.list.setFocus()
            QTest.keyClick(w.skills_panel.list, Qt.Key_Escape)  # ... and Esc pressed before it is released
            r["drag"] = (w._timeline_dragging, w.skill_timeline.is_editing())

        run_steps(app, [(10, setup), (1500, typing_while_recording), (100, held_handle_cancelled)])
        w._close_session(ask=False)

    assert r["recording"] and "Gown" in r["labels"]
    assert r["drag"] == (False, False)


def test_no_mouse_button_moves_the_frame_while_a_label_is_recorded(root):
    from qt_headless import headless_qt, run_steps

    with headless_qt() as app:
        from PyQt5.QtCore import QEvent, QPointF, Qt
        from PyQt5.QtGui import QMouseEvent
        from PyQt5.QtWidgets import QApplication

        import video_annote.ui.main_window as mw
        from video_annote.core.models import SkillStep

        w = mw.MainWindow(root_dir=root)
        w.skills_panel.set_skills([SkillStep(3, "Wear Gloves")])
        w.resize(1200, 800)
        w.show()
        r = {}

        def setup():
            w._open_session("S1")

        def press_middle():
            w.skills_panel.list.setCurrentRow(0)
            w._start_step()
            w._confirm_start()
            r["before"] = w.video_grid.position()
            slider = w.slider
            at = QPointF(slider.width() * 0.8, slider.height() / 2)
            for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
                QApplication.sendEvent(slider, QMouseEvent(kind, at, Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier))

        def check():
            r["after"] = w.video_grid.position()
            w._cancel_step()

        run_steps(app, [(10, setup), (1500, press_middle), (400, check)])
        w._close_session(ask=False)

    assert r["after"] == r["before"]


def test_a_created_session_is_the_one_offered_next(root, monkeypatch, tmp_path):
    from qt_headless import headless_qt

    from media_factory import make_media

    src = make_media(str(tmp_path / "cam.mp4"), duration=1, audio=None)
    with headless_qt():
        import video_annote.ui.main_window as mw
        from video_annote.sessions import SourceSpec
        from video_annote.ui.common import app_settings

        class Accepted:
            Accepted = 1

            def __init__(self, *_a):
                pass

            def exec_(self):
                return 1

            def session_label(self):
                return "S2"

            def sources(self):
                return [SourceSpec("video-1", src, "local")]

        monkeypatch.setattr(mw, "CreateSessionDialog", Accepted)
        monkeypatch.setattr(mw.MainWindow, "_show_session_info", lambda self, title="": None)
        w = mw.MainWindow(root_dir=root)
        w._create_new_session_dialog()
        assert w.state.label == "S2" and app_settings().value("last_session") == "S2"
        w._close_session(ask=False)


# ---------------- a clean UI ----------------

def test_new_session_takes_an_optional_recording_start(tmp_path, monkeypatch):
    from qt_headless import headless_qt

    from media_factory import make_media

    with_time = make_media(str(tmp_path / "screen.mov"), duration=1, audio=None,
                           extra=("-metadata", "creation_time=2026-03-20T16:37:15Z"))
    without = make_media(str(tmp_path / "cam.mp4"), duration=1, audio=None)
    with headless_qt():
        from PyQt5.QtCore import Qt
        from PyQt5.QtTest import QTest
        from PyQt5.QtWidgets import QDialogButtonBox, QMessageBox

        from video_annote.ui.dialogs.create_session import CreateSessionDialog

        asked = []
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes))
        dlg = CreateSessionDialog()
        dlg.label_edit.setText("S2")
        dlg._add_paths([with_time, without])
        rows = lambda: [dlg.list.itemWidget(dlg.list.item(i)).text() for i in range(dlg.list.count())]  # noqa: E731
        assert "· from the file" in rows()[0] and "Recording start not in the file" in rows()[1]
        assert dlg.list.currentRow() == 1 and dlg.start_box.title() == "Recording start of video-2 (optional)"
        field = dlg.start_edit
        field.setText("yesterday")
        assert "Type a Unix timestamp" in field.preview.text() and not dlg.btn_confirm.isEnabled()
        field.setText("1774024635203")  # milliseconds
        assert "read as milliseconds" in field.preview.text() and "16:37:15.203 UTC" in field.preview.text()
        QTest.keyClick(field.edit, Qt.Key_Return)  # confirms it, and does not start the import
        assert dlg.result() == 0 and "· entered" in rows()[1]
        assert [s.start.text for s in dlg.sources()] == ["", "1774024635203"]
        dlg.list.setCurrentRow(0)
        field.setText("1774024000")  # typed for video-1 but not confirmed: asked about at Import
        assert dlg.buttons.button(QDialogButtonBox.Ok).isEnabled()
        dlg._on_accept()
        assert asked and dlg.sources()[0].start.text == "1774024000" and dlg.result() == 1


def test_a_recording_start_can_be_typed_as_the_recording_software_wrote_it(tmp_path, monkeypatch, chicago):
    from qt_headless import headless_qt

    from media_factory import make_media

    cam = make_media(str(tmp_path / "20261026152732_0_c_merged.mp4"), duration=1, audio=None)  # an NVR's export
    with headless_qt():
        from PyQt5.QtWidgets import QInputDialog, QMessageBox

        from video_annote.media.recorded import EnteredTime
        from video_annote.ui.dialogs.create_session import CreateSessionDialog
        from video_annote.ui.widgets.start_time import StartTimeEdit

        def choose(combo, data):  # as a click in the list does
            combo.setCurrentIndex(combo.findData(data))
            combo.activated.emit(combo.currentIndex())

        dlg = CreateSessionDialog()
        dlg._add_paths([cam])
        field = dlg.start_edit
        assert field.form() == "unix" and field.zone_combo.isHidden()  # Unix time has no time zone
        field.setText("2026-10-26 15:27:32")
        assert "Choose “Year first” under Format" in field.preview.text() and not dlg.btn_confirm.isEnabled()
        field.setText("20261026152732")  # Unix milliseconds would be in 2612
        assert "in the future" in field.preview.text() and "choose “Year first”" in field.preview.text()
        choose(field.form_combo, "year-first")
        choose(field.zone_combo, "America/New_York")
        assert not field.zone_combo.isHidden()
        shown = field.preview.text()
        assert "Mon 26 Oct 2026, 15:27:32 (UTC−04:00)</b> US Eastern time" in shown and "14:27:32 here" in shown
        assert "19:27:32 UTC" in shown and dlg.btn_confirm.isEnabled()
        dlg._confirm_start()
        assert dlg.sources()[0].start == EnteredTime("20261026152732", "year-first", "America/New_York")
        assert "Starts Mon 26 Oct 2026, 14:27:32 (UTC−05:00) · entered" in dlg.list.itemWidget(dlg.list.item(0)).text()
        field.setText("2026-10-26T15:27:32-04:00")  # a time zone written in the time is used
        assert not field.zone_combo.isEnabled() and "15:27:32 (UTC−04:00)" in field.preview.text()
        field.setText("26/10/2026 15:27:32")
        assert "Choose “Day first” under Format" in field.preview.text()

        later = StartTimeEdit()  # the format and time zone chosen last are offered again
        assert (later.form(), later.zone(), later.text()) == ("year-first", "America/New_York", "")
        monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("europe/london", True)))
        choose(later.zone_combo, "other")
        assert later.zone() == "Europe/London" and later.zone_combo.currentText() == "Europe/London"
        warned = []
        monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("Mars/Olympus", True)))
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
        choose(later.zone_combo, "other")
        assert warned and later.zone() == "Europe/London"  # unchanged
        dlg.close()


def test_session_info_sets_a_recording_start_and_the_table_shows_the_essentials(root, monkeypatch):
    import json

    from qt_headless import headless_qt

    with headless_qt():
        import video_annote.ui.dialogs.session_info as si
        import video_annote.ui.main_window as mw

        w = mw.MainWindow(root_dir=root)
        w._open_session("S1")
        shown = [w.table.horizontalHeaderItem(c).text() for c in range(w.table.columnCount())
                 if not w.table.isColumnHidden(c)]
        assert shown == ["Step", "Name", "Start", "End", "Length", "Confidence", "Notes"]
        assert w.session_label.full_text() == "S1"  # the name only (its folder in the tooltip)
        from video_annote.media.recorded import EnteredTime
        monkeypatch.setattr(si, "ask_recording_start", lambda *a, **k: EnteredTime("1774024635"))
        dlg = si.SessionInfoDialog(w.state, [], parent=w, set_start=w._set_recording_start)
        from PyQt5.QtCore import QUrl
        dlg._on_link(QUrl("start:video-2"))
        assert "entered by hand" in dlg.browser.toPlainText()
        with open(f"{root}/S1/session.json", encoding="utf-8") as f:  # saved
            assert json.load(f)["videos"][1]["recorded"]["start_unix"] == 1774024635
        w._close_session(ask=False)
