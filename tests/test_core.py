"""Model, storage and time-code tests (no ffmpeg or GUI needed)."""
from __future__ import annotations

import dataclasses
import json
import os
from fractions import Fraction

import pytest

from video_annote.core import colors
from video_annote.core.models import (
    AnnotationRecord, RootConfig, SessionState, SkillStep, VideoItem, decode_camid, encode_camid,
)
from video_annote.core.storage import (
    TSV_HEADER, StorageError, escape_notes, list_sessions, load_annotations_tsv,
    load_root_config, load_session_state, move_to_trash, persist_session_state, save_annotations_tsv,
    save_root_config, unescape_notes, validate_session_label,
)
from video_annote.core.timecode import (
    Block, displayed_frame, recompute_from_frames, recompute_from_times, seconds_to_frames, snap_to_frame_start,
    stack_blocks_into_lanes,
)


def rec(start=1.0, end=2.0, notes="", step=3, name="Wear Gloves"):
    return AnnotationRecord(
        label="S1", camid="video-1,video-2", step_no=step, step_name=name,
        start_frame=seconds_to_frames(start, 25), end_frame=seconds_to_frames(end, 25),
        total_frames=seconds_to_frames(end, 25) - seconds_to_frames(start, 25),
        start_time=start, end_time=end, total_time=end - start,
        time_source="video-1", audio_source="video-1", confidence=7, notes=notes,
    )


# ---------------- label.tsv ----------------

@pytest.mark.parametrize("notes", [
    "", "plain", "line1\nline2", "tab\there", "back\\slash", "literal \\n stays", "crlf\r\nend", "ünïcødé ✓",
    "unicode separator\u0085and\x0cfeed",
])
def test_tsv_round_trip_is_lossless(tmp_path, notes):
    save_annotations_tsv(str(tmp_path), [rec(notes=notes)])
    loaded, skipped = load_annotations_tsv(str(tmp_path))
    assert skipped == []
    assert loaded == [rec(notes=notes)]


def test_tsv_layout_matches_0_1_contract(tmp_path):
    path = save_annotations_tsv(str(tmp_path), [rec(start=6.25, end=12.1333)])
    lines = open(path, encoding="utf-8").read().splitlines()
    assert lines[0].split("\t") == TSV_HEADER
    assert lines[1].split("\t") == [
        "S1", "video-1,video-2", "3", "Wear Gloves", "156", "303", "147",
        "6.250", "12.133", "5.883", "video-1", "video-1", "7", "",
    ]


def test_tsv_written_by_0_1_x_still_loads(tmp_path):
    # exact bytes as produced by video-annote 0.1.3
    (tmp_path / "label.tsv").write_text(
        "\t".join(TSV_HEADER) + "\n"
        "NG-DC-001\tvideo-1,video-2\t2\tHand Hygiene (Sanitizer or Wash)\t188\t364\t176\t6.250\t12.133\t"
        "5.883\tvideo-1\tvideo-1\t10\tfirst\\nsecond\n",
        encoding="utf-8",
    )
    loaded, skipped = load_annotations_tsv(str(tmp_path))
    assert skipped == []
    assert loaded[0].notes == "first\nsecond"
    assert (loaded[0].start_frame, loaded[0].end_frame, loaded[0].confidence) == (188, 364, 10)


def test_tsv_bad_rows_are_reported_and_backed_up_not_dropped(tmp_path):
    root, sdir = tmp_path, tmp_path / "S1"
    sdir.mkdir()
    save_annotations_tsv(str(sdir), [rec()])
    with open(sdir / "label.tsv", "a", encoding="utf-8") as f:
        f.write("S1\tvideo-1\tNOT_A_NUMBER\tx\t1\t2\t1\t0.1\t0.2\t0.1\tvideo-1\tvideo-1\t5\t\n")
    state, report = load_session_state(str(root), "S1")
    assert len(state.annotations) == 1
    assert report.skipped_rows and report.skipped_rows[0][0] == 3
    assert report.backups and os.path.exists(report.backups[0])
    assert "NOT_A_NUMBER" in open(report.backups[0], encoding="utf-8").read()


def test_tsv_columns_matched_by_name(tmp_path):
    cols = list(reversed(TSV_HEADER))
    values = dict(zip(TSV_HEADER, [
        "S1", "video-2", "4", "Name", "25", "50", "25", "1.000", "2.000", "1.000", "video-2", "video-1", "9", "n",
    ]))
    (tmp_path / "label.tsv").write_text("\t".join(cols) + "\n" + "\t".join(values[c] for c in cols) + "\n")
    loaded, _ = load_annotations_tsv(str(tmp_path))
    assert loaded[0].step_no == 4 and loaded[0].time_source == "video-2" and loaded[0].confidence == 9


def test_tabs_and_newlines_in_names_cannot_break_columns(tmp_path):
    r = rec(name="bad\tname\nhere")
    save_annotations_tsv(str(tmp_path), [r])
    line = open(tmp_path / "label.tsv", encoding="utf-8").read().splitlines()[1]
    assert len(line.split("\t")) == len(TSV_HEADER)


def test_notes_escape_is_backward_compatible_for_newlines():
    assert unescape_notes("a\\nb") == "a\nb"
    assert escape_notes("a\nb") == "a\\nb"


# ---------------- config / session ----------------

def test_root_config_round_trip_and_color_map(tmp_path):
    cfg = RootConfig(root_dir=str(tmp_path), skills=[SkillStep(2, "Hand Hygiene"), SkillStep(3, "Gloves")])
    colors.color_for_step(2, cfg.skill_color_map)
    colors.color_for_step(3, cfg.skill_color_map)
    save_root_config(cfg)
    loaded = load_root_config(str(tmp_path))
    assert loaded.skills == cfg.skills and loaded.skill_color_map == {2: 0, 3: 1}


def test_corrupt_config_is_backed_up_not_overwritten(tmp_path):
    (tmp_path / "config.json").write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(StorageError) as err:
        load_root_config(str(tmp_path))
    assert err.value.backup and open(err.value.backup).read() == "{ this is not json"


def test_config_from_windows_editors(tmp_path):
    text = json.dumps({"skills": [{"number": 1, "name": "Händewaschen"}]}, ensure_ascii=False)
    for encoding in ("utf-8-sig", "utf-16"):  # BOM / "Unicode" saves load as they are
        (tmp_path / "config.json").write_text(text, encoding=encoding)
        assert load_root_config(str(tmp_path)).skills == [SkillStep(1, "Händewaschen")]
    (tmp_path / "config.json").write_text(text, encoding="cp1252")  # not UTF-8: refused, but kept
    with pytest.raises(StorageError) as err:
        load_root_config(str(tmp_path))
    assert err.value.backup and open(err.value.backup, "rb").read() == text.encode("cp1252")


def test_config_with_one_bad_label_keeps_the_others(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"data_root": "/elsewhere", "skills": [{"number": 1, "name": "ok"}, {"name": "missing number"}]}))
    cfg = load_root_config(str(tmp_path))
    assert cfg.skills == [SkillStep(1, "ok")] and cfg.root_dir == str(tmp_path)


def test_session_round_trip_keeps_old_fields_and_view(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.videos = [VideoItem("video-1", "video-1.mp4", duration_ms=33000, fps=25.0, frame_rate="25/1",
                              frame_count=825, media={"audio": {"dual_mono": True}})]
    state.annotations = [rec()]
    state.active_view_ids, state.time_source_id, state.audio_source_id = ["video-1"], "video-1", "video-1"
    persist_session_state(state)
    meta = json.loads((sdir / "session.json").read_text())
    assert {"video_id", "filename", "source_type", "source", "duration_ms", "fps"} <= set(meta["videos"][0])
    loaded, report = load_session_state(str(tmp_path), "S1")
    assert loaded.videos[0].exact_fps == Fraction(25) and loaded.videos[0].media["audio"]["dual_mono"]
    assert loaded.annotations == [rec()] and loaded.time_source_id == "video-1"
    assert any("missing" in w for w in report.warnings)  # the mp4 itself was never written


def test_session_from_0_1_x_meta(tmp_path):
    sdir = tmp_path / "old"
    sdir.mkdir()
    (sdir / "session.json").write_text(json.dumps({"label": "old", "meta_version": 1, "videos": [
        {"video_id": "video-1", "filename": "video-1.mp4", "source_type": "local", "source": "/x.mp4",
         "duration_ms": 1000, "fps": 30.0}]}))
    state, _ = load_session_state(str(tmp_path), "old")
    assert state.videos[0].exact_fps == Fraction(30) and state.videos[0].media == {}


def test_session_with_unreadable_meta_decodes_notes_with_current_rules(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec(notes="col1\tcol2 C:\\data")]
    persist_session_state(state)
    (sdir / "session.json").write_text("{ truncated", encoding="utf-8")
    loaded, report = load_session_state(str(tmp_path), "S1")
    assert loaded.annotations[0].notes == "col1\tcol2 C:\\data" and report.backups


def test_concurrent_edits_are_kept_not_overwritten(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec()]
    assert persist_session_state(state) == []
    a, _ = load_session_state(str(tmp_path), "S1")  # the same session open in two windows
    b, _ = load_session_state(str(tmp_path), "S1")
    a.annotations.append(rec(start=3.0, end=4.0, notes="from A"))
    assert persist_session_state(a) == []
    b.annotations.append(rec(start=5.0, end=6.0, notes="from B"))
    kept = [k for k in persist_session_state(b) if os.path.basename(k).startswith("label.")]
    assert kept and "from A" in open(kept[0], encoding="utf-8").read()  # A's label is not lost
    assert "from B" in (sdir / "label.tsv").read_text(encoding="utf-8")
    assert persist_session_state(b) == []  # B's own saves are not reported again


def test_extra_tsv_columns_are_reported_and_kept_in_a_copy(tmp_path):
    sdir = tmp_path / "S1"
    sdir.mkdir()
    save_annotations_tsv(str(sdir), [rec()])
    header, row = (sdir / "label.tsv").read_text(encoding="utf-8").splitlines()
    (sdir / "label.tsv").write_text(f"{header}\tsplit\n{row}\ttrain\n", encoding="utf-8")
    state, report = load_session_state(str(tmp_path), "S1")
    assert state.annotations == [rec()]
    assert any("(split)" in w for w in report.warnings)
    assert report.backups and "train" in open(report.backups[0], encoding="utf-8").read()


@pytest.mark.parametrize("label,ok", [
    ("session_001", True), ("NG-DC-001", True), ("a b", True), ("", False), (" lead", False),
    ("trail.", False), (".hidden", False), ("a/b", False), ("a\\b", False), ("..", False), ("CON", False),
    ("x:y", False), ("tab\t", False),
])
def test_session_label_validation(label, ok):
    assert (validate_session_label(label) is None) is ok


def test_move_to_trash_never_deletes(tmp_path):
    sdir = tmp_path / "S1"
    (sdir / "tobii").mkdir(parents=True)
    (sdir / "tobii" / "gazedata.gz").write_bytes(b"precious")
    moved = move_to_trash(str(sdir), str(tmp_path))
    assert not sdir.exists() and open(os.path.join(moved, "tobii", "gazedata.gz"), "rb").read() == b"precious"
    assert list_sessions(str(tmp_path)) == []  # trash is hidden


def test_list_sessions_natural_detection(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "label.tsv").write_text("")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "video-1.mp4").write_bytes(b"")
    (tmp_path / "notes").mkdir()
    (tmp_path / ".a.importing-1").mkdir()
    assert list_sessions(str(tmp_path)) == ["a", "b"]


# ---------------- camid / time code / lanes ----------------

def test_camid_encoding():
    assert encode_camid(["Video-1", " video-2 ", "video-1", ""]) == "video-1,video-2"
    assert decode_camid(" video-1 , VIDEO-2,,") == ["video-1", "video-2"]


def test_displayed_frame_is_the_frame_on_screen():
    # frame k is visible during [k/25, (k+1)/25)
    assert [displayed_frame(t, 25) for t in (0.0, 0.039, 0.040, 1.019, 1.030)] == [0, 0, 1, 25, 25]
    assert displayed_frame(1.0, Fraction(30000, 1001)) == 29  # 29.97 fps: frame 30 starts at 1.001 s
    assert displayed_frame(0.999, 30) == 29 and displayed_frame(1.000, 30) == 30
    # frame-start times survive the 3-decimal TSV rounding (1/30 s is stored as 0.033)
    for k in range(0, 3000, 7):
        stored = round(k / 30, 3)
        assert displayed_frame(stored, 30) == k == seconds_to_frames(stored, 30)
        assert displayed_frame(round(snap_to_frame_start(k / 30 + 0.02, 30), 3), 30) == k


def test_recompute_round_trip_and_validation():
    r = recompute_from_times(rec(start=1.013, end=2.519), 25)
    assert (r.start_frame, r.end_frame, r.total_frames) == (25, 63, 38)
    r2 = recompute_from_frames(r, 25)
    assert (r2.start_time, r2.end_time) == (1.0, 2.52)
    with pytest.raises(ValueError):
        recompute_from_times(rec(start=2.0, end=1.0), 25)


def test_lanes_stack_overlaps_only():
    lanes = stack_blocks_into_lanes([Block(0, 0, 100), Block(1, 100, 200), Block(2, 50, 150), Block(3, 300, 250)])
    assert [[b.idx for b in lane] for lane in lanes] == [[0, 1, 3], [2]]


def test_colors_are_stable_and_distinct():
    cmap = {}
    first = [colors.color_for_step(n, cmap) for n in range(1, 60)]
    assert first[:3] == ["#E6194B", "#3CB44B", "#FFE119"]
    assert len(set(first)) == len(first)
    assert [colors.color_for_step(n, cmap) for n in range(1, 60)] == first


def test_saved_files_get_normal_permissions(tmp_path):
    import stat as st

    save_annotations_tsv(str(tmp_path), [rec()])
    mode = st.S_IMODE(os.stat(tmp_path / "label.tsv").st_mode)
    assert mode & 0o044, oct(mode)  # readable by group/others under the usual umask (not 0600)
    os.chmod(tmp_path / "label.tsv", 0o640)
    save_annotations_tsv(str(tmp_path), [rec()])
    assert st.S_IMODE(os.stat(tmp_path / "label.tsv").st_mode) == 0o640  # existing mode is kept


def test_export_folder_belongs_to_one_session(tmp_path):
    from video_annote.media.export import _check_owner, _session_identity

    def session(label, source):
        return SessionState(label=label, session_dir=str(tmp_path / label),
                            videos=[VideoItem("video-1", "video-1.mp4", source=source, frame_count=90)])

    mine = session("S", "/recordings/cam1.mp4")
    manifest = {"session": "S", "session_id": _session_identity(mine), "session_dir": "/another/root/S"}
    _check_owner("out", manifest, mine)  # a re-export, also from a copied or moved data root
    for other in (session("S", "/recordings/other-cam1.mp4"), session("T", "/recordings/cam1.mp4")):
        with pytest.raises(ValueError):  # same label from other recordings, or another label
            _check_owner("out", manifest, other)


def test_generated_colors_scale():
    import time

    assert len({colors.color_for_index(i) for i in range(300)}) == 300  # 0.1.x repeated greens after 50
    t = time.perf_counter()
    colors.color_for_index(50 + 5000)  # hand-edited configs can hold huge indices
    assert time.perf_counter() - t < 0.5


# ---------------- round-2 data-safety regressions ----------------

@pytest.mark.parametrize("eol,lead", [("\r", ""), ("\r\r\n", ""), ("\n", "\n  \n"), ("\r\n", "\r\n")])
def test_tsv_line_endings_and_leading_blank_lines(tmp_path, eol, lead):
    save_annotations_tsv(str(tmp_path), [rec(notes="a"), rec(start=3.0, end=4.0, notes="b")])
    lines = (tmp_path / "label.tsv").read_text(encoding="utf-8").splitlines()
    (tmp_path / "label.tsv").write_bytes((lead + eol.join(lines) + eol).encode("utf-8"))
    loaded, skipped = load_annotations_tsv(str(tmp_path))
    assert skipped == [] and [r.notes for r in loaded] == ["a", "b"]  # 0.1.3 read these; 0.2 must too


def test_tsv_rows_that_do_not_fit_are_reported_not_repaired(tmp_path):
    root, sdir = tmp_path, tmp_path / "S1"
    sdir.mkdir()
    save_annotations_tsv(str(sdir), [rec()])
    good = (sdir / "label.tsv").read_text(encoding="utf-8").splitlines()[1]
    with open(sdir / "label.tsv", "a", encoding="utf-8") as f:
        f.write(good + "\textra field\n")  # a raw tab inside a note
        f.write("second pair used\n")  # Enter pressed inside a note
        f.write(good.replace("\t3\tWear Gloves", "\t3.7\tWear Gloves") + "\n")  # step 3.7
    state, report = load_session_state(str(root), "S1")
    assert len(state.annotations) == 1
    assert [n for n, _ in report.skipped_rows] == [3, 4, 5]
    assert report.backups and "extra field" in open(report.backups[0], encoding="utf-8").read()


def test_session_folder_replaced_or_moved_is_never_written_into(tmp_path):
    from video_annote.core.storage import SessionMovedError, save_recovery_copy

    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec(notes="mine")]
    persist_session_state(state)
    loaded, _ = load_session_state(str(tmp_path), "S1")
    sdir.rename(tmp_path / "S1-renamed")  # renamed while open
    with pytest.raises(SessionMovedError):
        persist_session_state(loaded)
    assert not sdir.exists()  # not recreated
    other = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir),
                         videos=[VideoItem("video-1", "video-1.mp4", frame_rate="30/1", frame_count=90)])
    other.annotations = [rec(notes="new import")]
    persist_session_state(other)  # e.g. an import that replaced S1 meanwhile
    with pytest.raises(SessionMovedError):
        persist_session_state(loaded)
    assert "new import" in (sdir / "label.tsv").read_text(encoding="utf-8")
    kept = save_recovery_copy(loaded)
    assert "mine" in open(os.path.join(kept, "label.tsv"), encoding="utf-8").read()


def test_same_size_change_within_one_timestamp_tick_is_noticed(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec(notes="1")]
    persist_session_state(state)
    a, _ = load_session_state(str(tmp_path), "S1")
    tsv = sdir / "label.tsv"
    st = os.stat(tsv)
    tsv.write_text(tsv.read_text(encoding="utf-8").replace("\t1\n", "\t2\n"), encoding="utf-8")
    os.utime(tsv, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size, same timestamp (FAT32/HFS+)
    kept = [k for k in persist_session_state(a) if os.path.basename(k).startswith("label.")]
    assert kept and "\t2\n" in open(kept[0], encoding="utf-8").read()


def test_label_tsv_is_written_before_session_json(tmp_path, monkeypatch):
    import video_annote.core.storage as storage

    sdir = tmp_path / "old"
    sdir.mkdir()
    (sdir / "session.json").write_text(json.dumps({"label": "old", "meta_version": 1, "videos": []}))
    save_annotations_tsv(str(sdir), [rec(notes="NOTE")])
    tsv = sdir / "label.tsv"
    tsv.write_text(tsv.read_text(encoding="utf-8").replace("NOTE", "C:\\temp"), encoding="utf-8")  # 0.1.x: raw
    state, _ = load_session_state(str(tmp_path), "old")
    assert state.annotations[0].notes == "C:\\temp"
    real = storage.atomic_write_text

    def failing(path, text, encoding="utf-8"):
        if path.endswith("label.tsv"):
            raise OSError("disk full")
        real(path, text, encoding)

    monkeypatch.setattr(storage, "atomic_write_text", failing)
    with pytest.raises(OSError):
        persist_session_state(state)
    assert json.loads((sdir / "session.json").read_text())["meta_version"] == 1  # notes still 0.1.x-encoded
    monkeypatch.setattr(storage, "atomic_write_text", real)
    assert persist_session_state(state) == []  # no false "changed elsewhere" after the failed save
    assert load_session_state(str(tmp_path), "old")[0].annotations[0].notes == "C:\\temp"


def test_invalid_video_entries_are_kept_in_a_copy(tmp_path):
    sdir = tmp_path / "S1"
    sdir.mkdir()
    (sdir / "video-1.mp4").write_bytes(b"")
    (sdir / "session.json").write_text(json.dumps({"label": "S1", "meta_version": 2, "videos": [
        {"id": "video-1", "file": "video-1.mp4"}]}))
    state, report = load_session_state(str(tmp_path), "S1")
    assert [v.video_id for v in state.videos] == ["video-1"]  # inferred from the file names
    assert report.backups and any("could not be read" in w for w in report.warnings)


def test_config_is_written_only_when_changed_and_keeps_other_versions(tmp_path):
    cfg = RootConfig(root_dir=str(tmp_path), skills=[SkillStep(1, "a")])
    assert save_root_config(cfg) is None
    loaded = load_root_config(str(tmp_path))
    before = (tmp_path / "config.json").read_bytes()
    assert save_root_config(loaded) is None and (tmp_path / "config.json").read_bytes() == before  # unchanged
    other = load_root_config(str(tmp_path))
    other.skills.append(SkillStep(2, "added elsewhere"))
    save_root_config(other)
    loaded.skills.append(SkillStep(3, "added here"))
    kept = save_root_config(loaded)
    assert kept and "added elsewhere" in open(kept, encoding="utf-8").read()


def test_config_entries_that_cannot_be_read_are_reported_and_kept(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"skills": [{"number": 1, "name": "ok"}, {"number": 2.5, "name": "half"}, {"number": "2a", "name": "x"}],
         "skill_color_map": {"1": 0}}))
    cfg = load_root_config(str(tmp_path))
    assert cfg.skills == [SkillStep(1, "ok")] and len(cfg.problems) == 3  # 2 entries + where the copy is
    assert os.path.isdir(tmp_path / ".video-annote-backups")
    (tmp_path / "config.json").write_text(json.dumps({"skills": {"1": "Check"}}))
    assert load_root_config(str(tmp_path)).problems


# ---------------- round-3 data-safety regressions ----------------

def _legacy_session(tmp_path, note="C:\\temp"):
    sdir = tmp_path / "old"
    sdir.mkdir()
    (sdir / "session.json").write_text(json.dumps({"label": "old", "meta_version": 1, "videos": []}))
    save_annotations_tsv(str(sdir), [rec(notes="NOTE")])
    tsv = sdir / "label.tsv"
    tsv.write_text(tsv.read_text(encoding="utf-8").replace("NOTE", note), encoding="utf-8")  # 0.1.x: raw
    return sdir


def test_notes_survive_a_failed_session_json_write(tmp_path, monkeypatch):
    import video_annote.core.storage as storage

    sdir = _legacy_session(tmp_path)
    before = (sdir / "label.tsv").read_bytes()
    state, _ = load_session_state(str(tmp_path), "old")
    real = storage.atomic_write_text

    def failing(path, text, encoding="utf-8"):
        if path.endswith("session.json"):
            raise OSError("read-only file")
        real(path, text, encoding)

    monkeypatch.setattr(storage, "atomic_write_text", failing)
    with pytest.raises(OSError):
        persist_session_state(state)
    assert (sdir / "label.tsv").read_bytes() == before  # still the 0.1.x encoding session.json declares
    assert load_session_state(str(tmp_path), "old")[0].annotations[0].notes == "C:\\temp"


def test_a_window_that_only_colored_labels_keeps_the_other_windows_labels(tmp_path):
    save_root_config(RootConfig(root_dir=str(tmp_path), skills=[SkillStep(1, "a")]))
    viewer = load_root_config(str(tmp_path))
    editor = load_root_config(str(tmp_path))
    editor.skills.append(SkillStep(23, "Flush Tube"))
    save_root_config(editor)
    colors.color_for_step(23, viewer.skill_color_map)  # the viewer painted label 23 on its timeline
    assert save_root_config(viewer) is None  # nothing to warn about: only colors were added here
    on_disk = load_root_config(str(tmp_path))
    assert [s.number for s in on_disk.skills] == [1, 23] and 23 in on_disk.skill_color_map
    assert [s.number for s in viewer.skills] == [1, 23]  # the window now shows the other labels too


def test_unreadable_session_json_is_not_reported_as_changed_elsewhere(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec()]
    persist_session_state(state)
    (sdir / "session.json").write_text('{"label": "S1",, }', encoding="utf-8")
    loaded, report = load_session_state(str(tmp_path), "S1")
    assert report.backups  # copied aside when it was read
    assert persist_session_state(loaded) == []  # so the first save does not warn (or back it up) again


def test_values_in_an_unnamed_column_are_reported(tmp_path):
    root, sdir = tmp_path, tmp_path / "S1"
    sdir.mkdir()
    save_annotations_tsv(str(sdir), [rec(), rec(start=3.0, end=4.0)])
    header, a, b = (sdir / "label.tsv").read_text(encoding="utf-8").splitlines()
    (sdir / "label.tsv").write_text(f"{header}\t\r\n{a}\tQA: start late\r\n{b}\t\r\n", encoding="utf-8")
    state, report = load_session_state(str(root), "S1")
    assert len(state.annotations) == 2 and any("unnamed column 15" in w for w in report.warnings)
    assert report.backups and "QA: start late" in open(report.backups[0], encoding="utf-8").read()


def test_a_data_root_that_was_moved_is_never_recreated(tmp_path, monkeypatch):
    from video_annote.core.storage import save_recovery_copy

    root = tmp_path / "root"
    root.mkdir()
    cfg = RootConfig(root_dir=str(root), skills=[SkillStep(1, "a")])
    state = SessionState(root_dir=str(root), label="S1", session_dir=str(root / "S1"))
    root.rename(tmp_path / "root-renamed")
    with pytest.raises(StorageError):
        save_root_config(cfg)
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home") if p == "~" else p)
    kept = save_recovery_copy(state)
    assert not root.exists() and kept.startswith(str(tmp_path / "home"))


# ---------------- round-4 regressions ----------------

def test_an_unknown_frame_rate_stays_unknown_after_a_save(tmp_path):
    sdir = tmp_path / "S1"
    sdir.mkdir()
    (sdir / "video-1.mp4").write_bytes(b"")
    state, _ = load_session_state(str(tmp_path), "S1")  # no session.json: inferred, rate not measured
    assert not state.videos[0].rate_known
    persist_session_state(state)
    assert not load_session_state(str(tmp_path), "S1")[0].videos[0].rate_known  # not read back as 30 fps
    assert VideoItem.from_dict({"video_id": "v", "filename": "v.mp4"}).fps == 30.0  # 0.1.x: a missing rate


def test_label_frames_are_recomputed_from_the_times_when_they_disagree():
    from video_annote.sessions import frame_mismatch_note, sync_label_frames

    state = SessionState(videos=[VideoItem("video-1", "video-1.mp4", frame_rate="30000/1001")])
    copied = AnnotationRecord("S", "video-1", 1, "x", 120, 240, 120, 2.0, 4.0, 2.0, "video-1", "video-1")  # 60 fps
    fine = recompute_from_times(dataclasses.replace(copied, start_time=5.005, end_time=6.006), Fraction(30000, 1001))
    unknown = dataclasses.replace(copied, time_source="video-9")
    state.annotations = [copied, fine, unknown]
    rows = sync_label_frames(state)
    assert [(r.start_frame, r.end_frame, r.total_frames) for r in state.annotations] == [(60, 120, 60), (150, 180, 30),
                                                                                        (120, 240, 120)]
    assert state.annotations[0].start_time == 2.0 and rows == [1] and "(rows 1)" in frame_mismatch_note(rows, "x")
    assert sync_label_frames(state) == []


def test_a_config_merge_never_drops_entries_it_cannot_read(tmp_path):
    save_root_config(RootConfig(root_dir=str(tmp_path), skills=[SkillStep(1, "a")]))
    viewer = load_root_config(str(tmp_path))
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    data["skills"] += [{"number": 30, "name": "ok"}, {"number": "31b", "name": "typo"}]
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")  # edited by hand meanwhile
    colors.color_for_step(1, viewer.skill_color_map)
    kept = save_root_config(viewer)
    assert kept and "31b" in open(kept, encoding="utf-8").read()  # backed up and reported, not merged away


def test_a_config_merge_updates_the_color_map_in_place(tmp_path):
    save_root_config(RootConfig(root_dir=str(tmp_path), skills=[SkillStep(1, "a")]))
    viewer, editor = load_root_config(str(tmp_path)), load_root_config(str(tmp_path))
    editor.skills += [SkillStep(3, "c"), SkillStep(4, "d")]
    colors.color_for_step(3, editor.skill_color_map)
    colors.color_for_step(4, editor.skill_color_map)
    save_root_config(editor)
    shown = viewer.skill_color_map  # the label list widget holds this dict
    colors.color_for_step(2, shown)
    save_root_config(viewer)
    assert viewer.skill_color_map is shown and shown == load_root_config(str(tmp_path)).skill_color_map


def test_a_repeated_tsv_column_keeps_the_first_one(tmp_path):
    sdir = tmp_path / "S1"
    sdir.mkdir()
    save_annotations_tsv(str(sdir), [rec(notes="annotator note"), rec(start=3.0, end=4.0)])
    header, a, b = (sdir / "label.tsv").read_text(encoding="utf-8").splitlines()
    (sdir / "label.tsv").write_text(f"{header}\tnotes\tstart_time\n{a}\tQA row 1\t7.000\n{b}\t\t\n", encoding="utf-8")
    state, report = load_session_state(str(tmp_path), "S1")
    assert [(r.notes, r.start_time) for r in state.annotations] == [("annotator note", 1.0), ("", 3.0)]
    assert any("'notes' (column 15, repeated)" in w for w in report.warnings)


def test_a_failed_save_does_not_adopt_another_windows_label_tsv(tmp_path, monkeypatch):
    import video_annote.core.storage as storage

    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec()]
    persist_session_state(state)
    mine, _ = load_session_state(str(tmp_path), "S1")
    other, _ = load_session_state(str(tmp_path), "S1")
    other.annotations[0].notes = "other window"
    persist_session_state(other)
    real = storage.atomic_write_text

    def failing(path, text, encoding="utf-8"):
        if path.endswith("session.json"):
            raise OSError("locked")
        real(path, text, encoding)

    monkeypatch.setattr(storage, "atomic_write_text", failing)
    with pytest.raises(OSError):
        persist_session_state(mine)
    monkeypatch.setattr(storage, "atomic_write_text", real)
    backups = persist_session_state(mine)  # the retry: the other version is backed up and reported again
    assert any("other window" in open(b, encoding="utf-8").read() for b in backups if b.endswith(".tsv"))


def test_recovery_copies_never_recreate_a_renamed_root(tmp_path, monkeypatch):
    from video_annote.core.storage import save_recovery_copy

    root = tmp_path / "root"
    (root / "S1").mkdir(parents=True)
    state = SessionState(root_dir=str(root), label="S1", session_dir=str(root / "S1"))
    first = save_recovery_copy(state)
    assert first.startswith(str(root / ".video-annote-recovered"))
    root.rename(tmp_path / "root-renamed")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home") if p == "~" else p)
    second = save_recovery_copy(state, first)
    assert not root.exists() and second.startswith(str(tmp_path / "home"))


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can read any file")
def test_a_session_json_that_cannot_be_read_is_never_overwritten(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec()]
    persist_session_state(state)
    meta = sdir / "session.json"
    original = meta.read_bytes()
    meta.chmod(0)
    try:  # opening it with videos guessed from file names would lose their details on the next save
        with pytest.raises(StorageError, match="open the session again"):
            load_session_state(str(tmp_path), "S1")
    finally:
        meta.chmod(0o644)
    assert meta.read_bytes() == original



# ---------------- round-5 regressions ----------------

def test_annotation_records_round_trip_through_dicts_as_in_0_1_x():
    r = rec(notes="a\tb")
    assert AnnotationRecord.from_dict(r.to_dict()) == r


def test_an_unparsable_session_json_that_could_not_be_copied_is_not_replaced(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir))
    state.annotations = [rec()]
    persist_session_state(state)
    (sdir / "session.json").write_text('{"label": "S1",, }', encoding="utf-8")  # a hand edit gone wrong
    (sdir / ".video-annote-backups").write_text("not a folder", encoding="utf-8")  # copies cannot be made
    loaded, report = load_session_state(str(tmp_path), "S1")
    assert not report.backups
    with pytest.raises(OSError):
        persist_session_state(loaded)  # the save tries to keep it first, and refuses when it cannot
    assert (sdir / "session.json").read_text(encoding="utf-8") == '{"label": "S1",, }'


def test_validate_does_not_assume_30_fps_for_an_unknown_rate(tmp_path):
    from video_annote.sessions import validate_session

    sdir = tmp_path / "S1"
    sdir.mkdir()
    (sdir / "video-1.mp4").write_bytes(b"")
    state, _ = load_session_state(str(tmp_path), "S1")  # inferred: the rate was never measured
    state.annotations = [rec(1.04, 2.0)]  # frames 26..50 at 25 fps
    persist_session_state(state)
    report = validate_session(str(tmp_path), "S1", probe_media=False)
    assert any("does not state its frame rate" in w for w in report.warnings)
    assert not any("frame numbers do not match" in w for w in report.warnings)



# ---------------- round-6 regressions ----------------

def test_unreadable_entries_are_never_dropped_without_a_copy(tmp_path):
    sdir = tmp_path / "S1"
    state = SessionState(root_dir=str(tmp_path), label="S1", session_dir=str(sdir),
                         videos=[VideoItem("video-1", "video-1.mp4", frame_rate="25/1")])
    persist_session_state(state)
    meta = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    meta["videos"].append({"video_id": "video-3", "file": "video-3.mp4"})  # no 'filename': cannot be read
    (sdir / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    (sdir / ".video-annote-backups").write_text("not a folder", encoding="utf-8")  # copies cannot be made
    loaded, report = load_session_state(str(tmp_path), "S1")
    assert [v.video_id for v in loaded.videos] == ["video-1"] and not report.backups
    with pytest.raises(OSError):
        persist_session_state(loaded)  # it must be kept first
    assert "video-3" in (sdir / "session.json").read_text(encoding="utf-8")

    save_root_config(RootConfig(root_dir=str(tmp_path), skills=[SkillStep(1, "a")]))
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    data["skills"].append({"number": "two", "name": "b"})
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / ".video-annote-backups").write_text("not a folder", encoding="utf-8")
    cfg = load_root_config(str(tmp_path))
    assert "no copy could be made" in cfg.problems
    cfg.skills.append(SkillStep(3, "c"))
    with pytest.raises(OSError):
        save_root_config(cfg)
    assert '"two"' in (tmp_path / "config.json").read_text(encoding="utf-8")



def test_label_tsv_rows_that_cannot_be_read_are_not_dropped_without_a_copy(tmp_path):
    sdir = tmp_path / "S1"
    sdir.mkdir()
    save_annotations_tsv(str(sdir), [rec()])
    with open(sdir / "label.tsv", "a", encoding="utf-8") as f:
        f.write("S1\tvideo-1\tthree\tx\t0\t0\t0\t1.0\t2.0\t1.0\tvideo-1\tvideo-1\t5\t\n")  # step 'three'
    (sdir / ".video-annote-backups").write_text("not a folder", encoding="utf-8")
    state, report = load_session_state(str(tmp_path), "S1")
    assert report.skipped_rows and not report.backups
    with pytest.raises(OSError):
        persist_session_state(state)
    assert "three" in (sdir / "label.tsv").read_text(encoding="utf-8")


# ---------------- ease of use (0.2) ----------------

def test_typed_times_accept_the_readout_format():
    from video_annote.core.timecode import parse_time

    assert parse_time("64.5") == 64.5
    assert parse_time("01:04.500") == 64.5
    assert parse_time("61:04.5") == 3664.5  # minutes keep counting, as the readout shows them
    assert parse_time("1:01:04.5") == 3664.5
    for bad in ("", "1:", "1:75", "a:10", "1:2:3:4", "nan:1"):
        with pytest.raises(ValueError):
            parse_time(bad)


def test_session_info_is_plain_and_shows_recording_times(monkeypatch):
    import time
    from datetime import datetime, timezone

    from video_annote.core.models import SessionState, VideoItem
    from video_annote.media.recorded import local_text
    from video_annote.ui.dialogs.session_info import session_info_html

    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    try:
        a = VideoItem("video-1", "video-1.mp4", source="/x/Screen Recording 2026-03-20 at 11.37.15 AM.mov",
                      duration_ms=766800, frame_rate="60/1", frame_count=46008,
                      recorded={"start": "2026-03-20T11:37:15-05:00", "end": "2026-03-20T11:50:01-05:00",
                                "start_unix": 1774024635, "end_unix": 1774025401.8,
                                "from": "the file's creation time, to the second"},
                      notes=["The audio is very quiet."],
                      media={"video_start_s": 0.0, "audio": {"channels": 1, "sample_rate": 48000}})
        b = VideoItem("video-2", "video-2.mp4", source="C:\\rec\\cam2.mp4", duration_ms=1000,
                      recorded={"start": "2026-03-20T11:37:18.250-05:00", "start_unix": 1774024638.25,
                                "from": "entered by hand (Unix time 1774024638.25)"})
        c = VideoItem("video-3", "video-3.mp4", recorded={"start": None, "from": "not stated in the file"})
        text = session_info_html(SessionState(label="S", videos=[a, b, c]), ["a note"], editable=True)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert "Recorded 2026-03-20 11:37:15 → 11:50:01 (UTC−05:00), from the file" in text
    assert "Recorded 2026-03-20 11:37:18.250 (UTC−05:00), entered by hand" in text
    assert "Recording time not in the file" in text and "The audio is very quiet." in text
    assert "video-2 started 3 s after video-1" in text and "Set start…" in text and "a note" in text
    assert "Screen Recording 2026-03-20 at 11.37.15 AM.mov" in text and "cam2.mp4" in text
    for technical in ("Hz", "LUFS", "fps", "AAC", "ALAC", "H.264", "codec", "frames"):  # nothing technical
        assert technical not in text, technical
    assert local_text(datetime(2026, 3, 20, 16, 37, 15, tzinfo=timezone.utc), 1.0)  # (helper still in use)


def test_session_json_is_cleaned_of_import_diagnostics(tmp_path):
    from video_annote.core.models import VideoItem

    old = {"video_id": "video-1", "filename": "video-1.mp4", "source": "/x/a.mp4", "source_type": "local",
           "duration_ms": 1000, "fps": 25.0, "frame_rate": "25/1", "frame_count": 25,
           "media": {"width": 320, "height": 240, "video_start_s": 0.0, "source_summary": "h264 ...",
                     "audio": {"codec": "alac", "sample_rate": 48000, "channels": 1, "channel_layout": "mono",
                               "source_channel_layout": "stereo", "start_s": 0.0, "end_s": 1.0, "rms_dbfs": -30.0,
                               "loudness_lufs": -33.3, "hum_hz": 120, "dual_mono": True}},
           "ingest": {"plan": "video re-encoded", "ffmpeg": "ffmpeg version 8.0", "warnings": ["technical"]}}
    d = VideoItem.from_dict(old).to_dict()  # (as a session imported by a 0.2 preview is saved now)
    assert "ingest" not in d and d["media"] == {
        "video_start_s": 0.0, "audio": {"channels": 1, "channel_layout": "mono", "source_channel_layout": "stereo",
                                        "sample_rate": 48000, "start_s": 0.0, "end_s": 1.0, "dual_mono": True}}


def test_sessions_saved_by_a_preview_keep_their_notes_in_plain_words():
    from video_annote.core.models import VideoItem

    preview = {"video_id": "video-1", "filename": "video-1.mp4", "source": "/x/a.mp4", "duration_ms": 1000,
               "ingest": {"imported_at": "2026-10-27T11:27:32-0500", "warnings": [
                   "identical left/right channels stored once (exact mono)",
                   "steady 120 Hz hum/buzz locked to the mains frequency in the recording (harmonics 12 dB above ...)",
                   "the source's video has gaps (about 20 frames, 0.80 s, are missing); they are filled by repeating",
                   "source has 3 audio tracks; only the default one was kept"]}}
    v = VideoItem.from_dict(preview)
    assert v.notes == ["The audio has a steady 120 Hz electrical buzz (left as recorded).",
                       "About 0.8 s of video is missing from the recording; the picture holds still there.",
                       "The recording has 3 audio tracks; only the main one was kept."]
    assert v.imported == "2026-10-27 11:27"
