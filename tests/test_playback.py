"""VideoGrid against the real Qt multimedia backend (macOS AVFoundation), headless and silent.
Each check reflects a defect found in 0.1.x or in review. Pictures are checked, not only player
positions: every test frame paints its own index (see media_factory)."""
from __future__ import annotations

import sys

import pytest

pytestmark = [pytest.mark.media, pytest.mark.gui,
              pytest.mark.skipif(sys.platform != "darwin", reason="AVFoundation backend only")]


@pytest.fixture(scope="module")
def session_dir(tmp_path_factory):
    from media_factory import make_media
    from video_annote.media.ingest import ingest

    d = tmp_path_factory.mktemp("grid")
    make_media(str(d / "a.mp4"), duration=6, fps="25", audio=None)
    make_media(str(d / "b.mp4"), duration=4, fps="30", audio=None)
    ingest(str(d / "a.mp4"), str(d / "video-1.mp4"))
    ingest(str(d / "b.mp4"), str(d / "video-2.mp4"))
    return str(d)


def _items():
    from video_annote.core.models import VideoItem

    return [VideoItem("video-1", "video-1.mp4", duration_ms=6000, fps=25.0, frame_rate="25/1", frame_count=150),
            VideoItem("video-2", "video-2.mp4", duration_ms=4000, fps=30.0, frame_rate="30/1", frame_count=120)]


def _mapped(frame_25):
    """Frame of the 30 fps video that labels/exports use while the 25 fps time source shows frame_25."""
    from video_annote.core.timecode import displayed_frame

    return min(displayed_frame(round(frame_25 / 25, 3), 30), 119)


def test_video_grid_behaviour(session_dir):
    from qt_headless import headless_qt, run_steps, shown_frame

    with headless_qt() as app:
        from PyQt5.QtMultimedia import QMediaPlayer

        from video_annote.core.timecode import frame_at_ms
        from video_annote.ui.video_grid import VideoGrid

        grid = VideoGrid()
        grid.load(session_dir, _items())
        grid.set_active_views(["video-1", "video-2"])
        state = lambda: (grid.position(), {v: shown_frame(p) for v, p in grid._players.items()})  # noqa: E731
        r = {}
        run_steps(app, [
            (1500, lambda: r.update(ready=grid.is_ready())),
            (10, lambda: grid.seek(1234)),
            (400, lambda: r.update(seek=state())),
            (10, lambda: grid.step_frames(1)),
            (400, lambda: r.update(step=state())),
            (10, grid.play),
            (1100, grid.pause),
            (400, lambda: r.update(paused=state())),
            (10, lambda: grid.set_active_views(["video-1"])),  # hide video-2 (not clock, not audio)
            (10, lambda: grid.seek(500)),
            (300, grid.play),
            (700, lambda: r.update(hidden_idle=grid._players["video-2"].state() != QMediaPlayer.PlayingState)),
            (10, grid.pause),
            (400, lambda: r.update(hidden_paused=state())),
            (10, lambda: grid.set_active_views(["video-1", "video-2"])),
            (10, lambda: grid.seek(5700)),
            (200, grid.play),
            (1200, lambda: r.update(end=(grid.at_end(), grid.is_playing(), *state()))),
            (10, grid.pause),
            (400, lambda: r.update(pause_at_end=state())),
            (10, lambda: grid.set_time_source("video-2")),
            (400, lambda: r.update(switch=(grid.duration(), *state()))),
            (10, grid.clear),
        ])

    assert r["ready"]
    # 1234 ms is in frame 30 (1200-1240 ms): no whole-second truncation, and the picture is frame 30,
    # with the 30 fps video on the frame that exports use for t = 1.200 s
    pos, shown = r["seek"]
    assert frame_at_ms(pos, 25) == 30 and shown == {"video-1": 30, "video-2": _mapped(30)}
    pos, shown = r["step"]
    assert frame_at_ms(pos, 25) == 31 and shown == {"video-1": 31, "video-2": _mapped(31)}
    for key in ("paused", "hidden_paused"):  # paused anywhere: picture, position and other camera agree
        pos, shown = r[key]
        k = frame_at_ms(pos, 25)
        assert shown == {"video-1": k, "video-2": _mapped(k)}, key
    assert r["hidden_paused"][0] > 500
    assert r["hidden_idle"]  # hidden, non-audible players do not decode while playing
    at_end, playing, pos, shown = r["end"]
    assert at_end and not playing and frame_at_ms(pos, 25) == 149  # parked on the last frame, not rewound
    assert shown == {"video-1": 149, "video-2": 119}
    assert frame_at_ms(r["pause_at_end"][0], 25) == 149
    duration, pos, shown = r["switch"]  # clamped to the new master's last frame
    assert duration == 4000 and frame_at_ms(pos, 30) == 119 and shown["video-2"] == 119


@pytest.mark.parametrize("source,fps", [("video-1", 25), ("video-2", 30)])
def test_frame_steps_show_exactly_the_next_frame(session_dir, source, fps):
    """Seeking to a frame's exact start often showed the previous frame (Qt converts positions
    through float32), and at 30 fps the arrow keys could get stuck on one frame."""
    from qt_headless import headless_qt, run_steps, shown_frame

    with headless_qt() as app:
        from video_annote.core.timecode import frame_at_ms
        from video_annote.ui.video_grid import VideoGrid

        grid = VideoGrid()
        grid.load(session_dir, _items())
        grid.set_time_source(source)
        player = grid._players[source]
        seen = []
        steps = [(1500, lambda: None)]
        for _ in range(30):
            steps += [(10, lambda: grid.step_frames(1)),
                      (200, lambda: seen.append((frame_at_ms(grid.position(), fps), shown_frame(player))))]
        run_steps(app, steps + [(10, grid.clear)])

    assert seen == [(k, k) for k in range(1, 31)]


@pytest.fixture(scope="module")
def equal_dir(tmp_path_factory):
    from media_factory import make_media
    from video_annote.media.ingest import ingest

    d = tmp_path_factory.mktemp("equal")
    make_media(str(d / "a.mp4"), duration=3, fps="25", audio=None)
    make_media(str(d / "b.mp4"), duration=3, fps="30", audio=None)
    ingest(str(d / "a.mp4"), str(d / "video-1.mp4"))
    ingest(str(d / "b.mp4"), str(d / "video-2.mp4"))
    return str(d)


def test_cameras_that_reached_their_end_show_frames_again(session_dir, equal_dir):
    """Qt's macOS backend detaches the picture of a player that reaches its end; seeking alone did
    not bring it back, so a paused camera stayed frozen (or black) after playing past its end."""
    from qt_headless import headless_qt, run_steps, shown_frame

    with headless_qt() as app:
        from video_annote.core.models import VideoItem
        from video_annote.core.timecode import frame_at_ms
        from video_annote.ui.video_grid import VideoGrid

        grid, equal = VideoGrid(), VideoGrid()
        grid.load(session_dir, _items())  # video-2 (4 s) is shorter than the time source (6 s)
        grid.set_active_views(["video-1", "video-2"])
        equal.load(equal_dir, [VideoItem("video-1", "video-1.mp4", frame_rate="25/1", frame_count=75),
                               VideoItem("video-2", "video-2.mp4", frame_rate="30/1", frame_count=90)])
        equal.set_active_views(["video-1", "video-2"])

        def state(g):
            return g.position(), {v: shown_frame(p) for v, p in g._players.items()}

        black = lambda: grid._cells["video-2"].is_black()  # noqa: E731
        r = {}
        run_steps(app, [
            (1500, lambda: grid.seek(3600)),
            (200, grid.play),
            (1100, grid.pause),  # video-2 ended at 4.0 s while playing
            (300, lambda: r.update(black_after_end=black())),
            (10, lambda: grid.seek(2000)),
            (400, lambda: r.update(back=state(grid), black_back=black())),
            (10, lambda: grid.step_frames(-3)),
            (400, lambda: r.update(step=state(grid))),
            (10, lambda: grid.seek(99 * 40 + 20)),  # master frame 99: video-2's last frame (118) is used
            (300, lambda: r.update(black_99=black())),
            (10, lambda: grid.seek(100 * 40 + 20)),  # frame 100 maps past video-2's end
            (300, lambda: r.update(black_100=black())),
            (10, lambda: equal.seek(2400)),
            (200, equal.play),
            (1200, lambda: r.update(equal_end=equal.at_end())),  # both reach their end together
            (10, lambda: equal.seek(1000)),
            (400, lambda: r.update(equal_back=state(equal))),
            (10, grid.clear),
            (10, equal.clear),
        ])

    assert r["black_after_end"] and not r["black_back"]
    for key in ("back", "step"):
        pos, shown = r[key]
        k = frame_at_ms(pos, 25)
        assert shown == {"video-1": k, "video-2": _mapped(k)}, key
    assert not r["black_99"] and r["black_100"]
    assert r["equal_end"]
    pos, shown = r["equal_back"]
    k = frame_at_ms(pos, 25)
    assert k == 25 and shown == {"video-1": 25, "video-2": _mapped(25)}


@pytest.fixture(scope="module")
def odd_dir(tmp_path_factory):
    from media_factory import make_media
    from video_annote.media.ingest import ingest

    d = tmp_path_factory.mktemp("odd")
    for name, kw in (("m25", dict(duration=6, fps="25")), ("c119", dict(duration=3.95, fps="30")),
                     ("m30", dict(duration=4, fps="30")), ("l25", dict(duration=8, fps="25"))):
        make_media(str(d / f"{name}-src.mp4"), audio=None, **kw)
        ingest(str(d / f"{name}-src.mp4"), str(d / f"{name}.mp4"))
    return str(d)


def test_black_cells_and_end_parking_follow_the_export(odd_dir):
    from qt_headless import headless_qt, run_steps, shown_frame

    with headless_qt() as app:
        from video_annote.core.models import VideoItem
        from video_annote.ui.video_grid import VideoGrid

        def item(name, rate, frames):
            return VideoItem(name, f"{name}.mp4", frame_rate=rate, frame_count=frames)

        short = VideoGrid()  # 25 fps time source with a 119-frame 30 fps camera (it ends at 3.967 s)
        short.load(odd_dir, [item("m25", "25/1", 150), item("c119", "30/1", 119)])
        short.set_active_views(["m25", "c119"])
        longer = VideoGrid()  # a 30 fps time source of 4 s with a longer 25 fps camera
        longer.load(odd_dir, [item("m30", "30/1", 120), item("l25", "25/1", 200)])
        longer.set_active_views(["m30", "l25"])
        black = lambda: short._cells["c119"].is_black()  # noqa: E731
        r = {}
        run_steps(app, [
            (1500, lambda: short.seek(99 * 40 + 20)),  # frame 99 (3.960 s): c119 still has frame 118
            (400, lambda: r.update(black_99=black(), shown_99=shown_frame(short._players["c119"]))),
            (10, lambda: short.seek(100 * 40 + 20)),  # frame 100 (4.000 s): c119 has ended
            (400, lambda: r.update(black_100=black())),
            (10, lambda: longer.seek(3010)),
            (100, longer.play),
            (1600, lambda: r.update(end=longer.at_end(), shown_end=shown_frame(longer._players["l25"]))),
            (10, short.clear),
            (10, longer.clear),
        ])

    assert not r["black_99"] and r["shown_99"] == 118 and r["black_100"]
    # at the end (frame 119 = 3.967 s) the 25 fps camera shows frame 99, which its clips use
    assert r["end"] and r["shown_end"] == 99
