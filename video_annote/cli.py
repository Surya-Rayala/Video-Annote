# video_annote/cli.py
"""Command line entry point.

    video-annote                      start the annotation GUI
    video-annote gui [--root DIR]
    video-annote probe FILE...        inspect recordings (streams, audio levels, import plan)
    video-annote import --root DIR --session LABEL SOURCE...
    video-annote export --root DIR --session LABEL [--out DIR] ...
    video-annote validate --root DIR [--session LABEL ...]

Exit codes: 0 success; 1 the command ran but found problems or failed (validation errors, clips that
failed, nothing to export, an import that failed); 2 it could not start (bad arguments, a data root or
session that does not exist, an existing session without --replace, ffmpeg not found).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__


def _progress_printer(prefix: str):
    last = {"pct": -1}

    def report(fraction: float, message: str = "") -> None:
        pct = int(fraction * 100)
        if pct != last["pct"]:
            last["pct"] = pct
            print(f"\r{prefix} {pct:3d}%  {message[:70]:<70}", end="", file=sys.stderr, flush=True)
            if fraction >= 1.0:
                print(file=sys.stderr)

    return report


def cmd_gui(args: argparse.Namespace) -> int:
    from .app import run_app
    return run_app(root_dir=args.root)


def cmd_probe(args: argparse.Namespace) -> int:
    from .media.analysis import analyze_audio
    from .media.ingest import _cadence, _first_frame_times, _hls_playlist, _lost_priming, keeps_timestamps, plan_ingest
    from .media.probe import probe
    from .media.recorded import find_recording_time, recorded_block

    from .media.ffmpeg import FFmpegNotFoundError

    status = 0
    for path in args.files:
        print(f"== {path}")
        if "://" not in path and not os.path.exists(path):
            print("   ERROR: no such file")
            status = 2
            continue
        try:
            info = probe(path)
            print(f"   {info.summary()}")
            stats = (analyze_audio(path, info.audio.channels, stream_index=info.audio.index)
                     if info.audio is not None else None)  # the track an import would keep
            cadence = _cadence(_first_frame_times(path, info, 120.0))  # as an import plans it
            plan = plan_ingest(info, dual_mono=bool(stats and stats.dual_mono), cadence=cadence,
                               lost_priming_s=_lost_priming(path, info, 120.0))
            print(f"   import plan: {plan.describe()}")
            for note in plan.notes:
                print(f"   note: {note}")
            playlist = _hls_playlist(path, info, 120.0) if "hls" in info.format_name else None
            stamp = find_recording_time(path, info, playlist.program_date_time if playlist else None)
            origin = 0.0 if keeps_timestamps(info) else info.start_time  # as an import places t=0
            v = info.video
            end = v.start_time + v.duration - origin if v is not None and v.duration else None
            rec = recorded_block(stamp, origin, info.start_time, end)
            if rec["start"]:
                print(f"   recorded: {rec['start']} to {rec.get('end', '?')} (Unix {rec['start_unix']} to "
                      f"{rec.get('end_unix', '?')}; from {rec['from']})")
            else:
                print(f"   recorded: {rec['from']}")
            if stats is not None:
                print(f"   audio: RMS {stats.rms_dbfs} dBFS, peak {stats.peak_dbfs} dBFS, "
                      f"loudness {stats.loudness_lufs} LUFS, leading silence {stats.leading_silence_s} s, "
                      f"dual-mono {stats.dual_mono}, hum {stats.hum_hz or 'none'}")
                for w in stats.warnings(stored_as_alac=plan.audio_action == "lossless"):
                    print(f"   warning: {w}")
        except FFmpegNotFoundError:
            raise  # nothing can be probed: exit 2
        except Exception as e:  # report and continue with the next file
            print(f"   ERROR: {e}")
            status = max(status, 1)
    return status


def cmd_import(args: argparse.Namespace) -> int:
    from .core.storage import session_exists, validate_session_label
    from .media.ingest import IngestOptions
    from .media.recorded import EnteredTime, read_entered
    from .sessions import SourceSpec, create_session, is_url, validate_local_video_path, validate_video_url

    if not os.path.isdir(args.root):  # a typo must not create a new data root
        print(f"error: data root {args.root} does not exist (create the folder first)", file=sys.stderr)
        return 2
    problem = validate_session_label(args.session)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    if session_exists(args.root, args.session)[0] and not args.replace:
        print(f"error: session '{args.session}' already exists in {args.root} (use --replace to replace it)",
              file=sys.stderr)
        return 2
    starts = {}
    for given in args.start or []:  # N=TIME: when video N started (optional)
        number, _, value = given.partition("=")
        entered = EnteredTime(value.strip(), args.start_format, args.time_zone)
        try:
            if not number.strip().isdigit() or not 1 <= int(number) <= len(args.sources):
                raise ValueError(f"no video {number.strip()!r} (videos are numbered 1 to {len(args.sources)})")
            read_entered(entered)
        except ValueError as e:
            print(f"error: --start {given}: {e}", file=sys.stderr)
            return 2
        starts[int(number)] = entered
    specs: List[SourceSpec] = []
    for i, src in enumerate(args.sources, start=1):
        kind = "url" if is_url(src) else "local"
        ok, msg = validate_video_url(src) if kind == "url" else validate_local_video_path(src)
        if not ok:
            print(f"error: {msg}", file=sys.stderr)
            return 2
        specs.append(SourceSpec(f"video-{i}", os.path.abspath(src) if kind == "local" else src, kind,
                                starts.get(i, EnteredTime(""))))
    options = IngestOptions(crf=args.crf, preset=args.preset, audio_mode=args.audio)
    result = create_session(args.root, args.session, specs, replace=args.replace, options=options,
                            on_progress=_progress_printer("import"))
    for n in result.notes:
        print(f"note: {n}")
    if args.verbose:
        for w in result.warnings:
            print(f"detail: {w}")
    if result.replaced_to:
        print(f"previous session moved to {result.replaced_to}")
    print(f"created {result.state.session_dir}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .core.storage import StorageError, load_session_state, session_dir
    from .media.export import ExportOptions, export_segments

    if args.no_video and args.no_wav:
        print("error: --no-video and --no-wav together leave nothing to export", file=sys.stderr)
        return 2
    if not os.path.isdir(session_dir(args.root, args.session)):
        print(f"error: no session '{args.session}' in {args.root}", file=sys.stderr)
        return 2
    try:
        state, load = load_session_state(args.root, args.session, backup=False)
    except (StorageError, OSError) as e:  # e.g. a session.json or label.tsv that cannot be read
        print(f"error: {e}", file=sys.stderr)
        return 2
    for w in load.warnings:
        print(f"warning: {w}")
    if load.skipped_rows:
        print(f"error: label.tsv has unreadable rows {[n for n, _ in load.skipped_rows]}", file=sys.stderr)
        return 2
    if not state.videos:
        print(f"error: session '{args.session}' has no videos", file=sys.stderr)
        return 2
    if not state.annotations:
        print(f"error: session '{args.session}' has no annotations to export", file=sys.stderr)
        return 1
    options = ExportOptions(
        out_dir=args.out or os.path.join(args.root, "exports"), cameras=args.cameras,
        write_video=not args.no_video, write_wav=not args.no_wav, audio_channels=args.audio_channels,
        audio_sample_rate=args.sample_rate, wav_format=args.wav_format, crf=args.crf, preset=args.preset,
        workers=args.workers,
    )
    report = export_segments(state, options, on_progress=_progress_printer("export"))
    for w in report.warnings:
        print(f"warning: {w}")
    for r in report.failed:
        print(f"FAILED {r.job.out_path}: {r.error}")
    print(f"{len(report.results) - len(report.failed)}/{len(report.results)} files written; "
          f"manifest: {report.manifest_csv or '-'}")
    if not report.results:
        sys.stdout.flush()  # the warnings first, also when both streams go to one pipe
        print("error: nothing was exported: every output was skipped (see the warnings)", file=sys.stderr)
    return 1 if report.failed or not report.results else 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .core.storage import StorageError, list_sessions, load_root_config, session_dir
    from .media.ffmpeg import find_toolchain
    from .sessions import validate_session

    if not os.path.isdir(args.root):
        print(f"error: data root {args.root} does not exist", file=sys.stderr)
        return 2
    missing = [label for label in args.session or [] if not os.path.isdir(session_dir(args.root, label))]
    if missing:
        print(f"error: no session {', '.join(repr(m) for m in missing)} in {args.root}", file=sys.stderr)
        return 2
    if not args.fast:
        find_toolchain()  # raises FFmpegNotFoundError (exit 2) instead of failing every session
    try:
        cfg = load_root_config(args.root, backup=False)
    except StorageError as e:
        print(f"warning: {e}")
        cfg = None
    labels = args.session or list_sessions(args.root)
    if not labels:
        print(f"error: no sessions found in {args.root}", file=sys.stderr)
        return 2
    failed = 0
    for label in labels:
        try:
            rep = validate_session(args.root, label, cfg, probe_media=not args.fast)
        except Exception as e:  # report and keep checking the other sessions
            print(f"== {label}: ERRORS (could not be checked: {e})")
            failed += 1
            continue
        print(f"== {label}: {'OK' if rep.ok else 'ERRORS'} ({len(rep.errors)} errors, {len(rep.warnings)} warnings)")
        for e in rep.errors:
            print(f"   error: {e}")
        for w in rep.warnings:
            print(f"   warning: {w}")
        failed += not rep.ok
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    from .media.recorded import START_FORMS

    p = argparse.ArgumentParser(prog="video-annote", description="Multi-video annotation tool.")
    p.add_argument("--version", action="version", version=f"video-annote {__version__}")
    sub = p.add_subparsers(dest="command")

    g = sub.add_parser("gui", help="start the annotation GUI (default)")
    g.add_argument("--root", help="data root folder to open")
    g.set_defaults(func=cmd_gui)

    pr = sub.add_parser("probe", help="inspect recordings and show how they would be imported")
    pr.add_argument("files", nargs="+")
    pr.set_defaults(func=cmd_probe)

    im = sub.add_parser("import", help="create a session from recordings without the GUI")
    im.add_argument("--root", required=True)
    im.add_argument("--session", required=True, help="session label (folder name)")
    im.add_argument("--replace", action="store_true",
                    help="replace an existing session (moved to the trash once the import succeeded)")
    im.add_argument("--crf", type=int, default=18)
    im.add_argument("--preset", default="veryfast")
    im.add_argument("--audio", choices=("auto", "aac"), default="auto",
                    help="auto: copy AAC, store other audio losslessly as ALAC (default); "
                         "aac: re-encode to AAC for smaller files")
    im.add_argument("--start", action="append", metavar="N=TIME",
                    help="when video N started recording (repeatable), written as --start-format says. "
                         "Without it the time the file states is used, if any")
    im.add_argument("--start-format", choices=tuple(START_FORMS), default="unix",
                    help="how --start times are written: unix (seconds since 1970, or milliseconds; the "
                         "default), year-first (20261026152732, 2026-10-26 15:27:32), month-first "
                         "(10/26/2026 3:27:32 PM) or day-first (26/10/2026 15:27:32)")
    im.add_argument("--time-zone", default="", metavar="ZONE",
                    help="time zone of --start dates and times that state none, e.g. America/New_York "
                         "(default: this computer's)")
    im.add_argument("-v", "--verbose", action="store_true", help="also print technical import details")
    im.add_argument("sources", nargs="+", help="video files or http(s) URLs, in camera order")
    im.set_defaults(func=cmd_import)

    ex = sub.add_parser("export", help="export labeled clips (MP4 + WAV) and a manifest")
    ex.add_argument("--root", required=True)
    ex.add_argument("--session", required=True)
    ex.add_argument("--out", help="output folder; files go to OUT/<session>/ (default: <root>/exports). That "
                                  "folder mirrors the latest export: files of an earlier export into it that this "
                                  "one does not produce (deleted labels, outputs or cameras left out) are removed")
    ex.add_argument("--cameras", choices=("annotated", "all", "time_source"), default="annotated",
                    help="annotated: the views visible when each label was recorded (default); all: every video; "
                         "time_source: each label's time source only")
    ex.add_argument("--no-video", action="store_true", help="do not write MP4 clips")
    ex.add_argument("--no-wav", action="store_true", help="do not write WAV files")
    ex.add_argument("--audio-channels", choices=("auto", "mono", "keep"), default="auto",
                    help="WAV channels: auto = mono for mono and identical-channel sources, else as recorded "
                         "(default); mono = downmix; keep. MP4 clips keep the recorded audio")
    ex.add_argument("--sample-rate", type=int, default=None,
                    help="resample the WAVs, e.g. 16000 (default: the recorded rate; MP4 clips keep it)")
    ex.add_argument("--wav-format", choices=("s16", "s24", "f32"), default="s16",
                    help="WAV sample format: s16 (default, readable by every tool), s24, or f32 (the decoded "
                         "audio exactly, including peaks above full scale)")
    ex.add_argument("--crf", type=int, default=18, help="H.264 quality of the clips (lower is better; default 18)")
    ex.add_argument("--preset", default="veryfast", help="x264 speed preset of the clips (default veryfast)")
    ex.add_argument("--workers", type=int, default=2, help="clips encoded in parallel (default 2)")
    ex.set_defaults(func=cmd_export)

    va = sub.add_parser("validate", help="check sessions for missing files and inconsistent labels")
    va.add_argument("--root", required=True)
    va.add_argument("--session", action="append", help="session label (repeatable; default: all)")
    va.add_argument("--fast", action="store_true", help="skip probing video files")
    va.set_defaults(func=cmd_validate)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args.root = None
        return cmd_gui(args)
    from .media.ffmpeg import FFmpegNotFoundError

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FFmpegNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
