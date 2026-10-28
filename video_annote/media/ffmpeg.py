# video_annote/media/ffmpeg.py
"""Locating and running ffmpeg / ffprobe.

Everything that spawns an ffmpeg process goes through this module so that binary discovery,
progress reporting, cancellation, timeouts and error messages behave the same everywhere.
"""
from __future__ import annotations

import functools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

INSTALL_HINT = (
    "ffmpeg and ffprobe are required but were not found.\n"
    "Install them (for example `conda install -c conda-forge ffmpeg`, `brew install ffmpeg`, "
    "or `winget install ffmpeg`) or point FFMPEG_BIN / FFPROBE_BIN at the executables."
)

# Hide the console window that would otherwise flash up for every ffmpeg call on Windows.
_POPEN_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

ProgressCallback = Callable[[float], None]
CancelCheck = Callable[[], bool]


class FFmpegNotFoundError(RuntimeError):
    """ffmpeg or ffprobe could not be located."""


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe process failed. ``details`` holds the tail of its log."""

    def __init__(self, message: str, details: str = "") -> None:
        hint = explain(details)
        if hint:
            message = f"{message}: {hint}"
        super().__init__(message if not details else f"{message}\n\n{details}")
        self.summary = message
        self.details = details


class CancelledError(Exception):
    """The operation was cancelled by the user."""


@dataclass(frozen=True)
class Toolchain:
    ffmpeg: str
    ffprobe: str


def _candidate_dirs() -> List[Path]:
    """Directories searched after PATH. GUI launches on macOS often get a minimal PATH, so we also
    look next to the running interpreter (conda/venv installs) and in the usual package-manager prefixes."""
    dirs = [Path(sys.prefix) / "bin", Path(sys.prefix) / "Library" / "bin", Path(sys.prefix) / "Scripts"]
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        dirs += [Path(conda_prefix) / "bin", Path(conda_prefix) / "Library" / "bin"]
    dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
    return dirs


def _resolve(name: str, env_var: str) -> Optional[str]:
    override = os.environ.get(env_var, "").strip()
    if override:
        found = shutil.which(override)
        if found:
            return found
        return override if Path(override).is_file() else None
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    for d in _candidate_dirs():
        p = d / exe
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


@functools.lru_cache(maxsize=1)
def find_toolchain() -> Toolchain:
    ffmpeg = _resolve("ffmpeg", "FFMPEG_BIN")
    ffprobe = _resolve("ffprobe", "FFPROBE_BIN")
    if not ffmpeg or not ffprobe:
        raise FFmpegNotFoundError(INSTALL_HINT)
    return Toolchain(ffmpeg=ffmpeg, ffprobe=ffprobe)


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    """First line of `ffmpeg -version` (recorded in session/export metadata for reproducibility)."""
    out = subprocess.run(
        [find_toolchain().ffmpeg, "-hide_banner", "-version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, creationflags=_POPEN_FLAGS,
    )
    first = (out.stdout or "").splitlines()
    return first[0].strip() if first else "unknown"


_HINTS = (
    ("moov atom not found", "The file is incomplete or not a finished MP4 (still recording or copying?)."),
    ("Invalid data found when processing input", "The file is damaged or not a supported video format."),
    ("No such file or directory", "The file does not exist (moved, renamed or on a disconnected drive?)."),
    ("Permission denied", "Permission denied: check that the file and the data root are readable/writable."),
    ("No space left on device", "The disk is full."),
    ("Connection refused", "The server refused the connection."),
    ("Connection timed out", "The network connection timed out."),
    ("Server returned 4", "The server rejected the request (the link may have expired or need a login)."),
    ("Server returned 5", "The server had an error; try again later."),
    ("does not contain any stream", "The file contains no audio or video streams."),
)


def explain(log: str) -> str:
    """A one-line, human-readable cause for an ffmpeg log, when we recognise it."""
    for needle, hint in _HINTS:
        if needle in log:
            return hint
    return ""


@functools.lru_cache(maxsize=1)
def ffmpeg_release() -> Optional[Tuple[int, int]]:
    """(major, minor) of the ffmpeg release, or None for development builds (assumed recent)."""
    m = re.match(r"ffmpeg version n?(\d+)\.(\d+)", ffmpeg_version())
    return (int(m.group(1)), int(m.group(2))) if m else None


@functools.lru_cache(maxsize=1)
def _mp4_has_movie_timescale() -> bool:
    """Whether ffmpeg's MP4 muxer takes -movie_timescale (not in 4.4)."""
    try:
        out = run_capture([find_toolchain().ffmpeg, "-hide_banner", "-h", "muxer=mp4"], timeout=30)
    except FFmpegError:
        return False
    return "movie_timescale" in (out.stdout or "") + (out.stderr or "")


def _compat(args: Sequence[str]) -> List[str]:
    """Adapt arguments to older ffmpeg releases: -fps_mode exists from 5.1, -vsync before that; an MP4
    muxer without -movie_timescale keeps its default (edit lists in milliseconds)."""
    out = list(args)
    if "-movie_timescale" in out and not _mp4_has_movie_timescale():
        i = out.index("-movie_timescale")
        del out[i:i + 2]
    release = ffmpeg_release()
    if release is None or release >= (5, 1):
        return out
    return ["-vsync" if a == "-fps_mode" else a for a in out]


def _tail(lines: Sequence[str], n: int = 25) -> str:
    useful = [line.rstrip() for line in lines if line.strip()]
    return "\n".join(useful[-n:])


def run_capture(cmd: Sequence[str], timeout: Optional[float] = 120.0) -> subprocess.CompletedProcess:
    """Run a short-lived tool invocation and capture its text output."""
    try:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=timeout, creationflags=_POPEN_FLAGS,
        )
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"{Path(cmd[0]).name} timed out after {timeout:.0f} s") from e


def ffprobe_json(target: str, *args: str, timeout: Optional[float] = 120.0) -> Dict:
    """Run ffprobe with JSON output and return the parsed document."""
    tools = find_toolchain()
    cmd = [tools.ffprobe, "-v", "error", "-of", "json", *args, target]
    proc = run_capture(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe could not read {target}", _tail(proc.stderr.splitlines()))
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise FFmpegError(f"ffprobe returned invalid JSON for {target}") from e


# ffmpeg runs with "-loglevel level+verbose", which tags every log line with its severity, e.g.
# "[h264 @ 0x7f..] [error] Invalid NAL unit size". Only warnings and errors are inspected: info lines
# repeat file names, URLs and metadata, which can contain any word.
_LEVEL_TAG = re.compile(r"\[(trace|debug|verbose|info|warning|error|fatal|panic)\] ")
_FPS_STATS = re.compile(r"(\d+) frames in, \d+ frames out; (\d+) frames dropped, (\d+) frames duplicated")
# Warnings/errors meaning that input data was lost (the output would silently contain a gap or freeze).
# While the input is still being probed, only segment failures count: an HTTP error there can belong
# to a playlist variant that is not used.
SEGMENT_LOSS_MARKERS = ("Failed to open segment", "Failed to reload playlist")
LOSS_MARKERS = SEGMENT_LOSS_MARKERS + ("HTTP error", "Server returned 4", "Server returned 5",
                                       "Stream ends prematurely")
# ... except these: a body without Content-Length ends "prematurely" against an unknown size, and a
# kept-alive connection that was reset is retried on a new one.
NOT_LOSS = ("should be 18446744073709551615", "retrying with new connection")
# Error-level messages that do not mean damage: ffmpeg 8.0's Opus parser logs the first for every
# Opus track in Matroska/WebM; the others end complete HTTP downloads (a body without Content-Length,
# chunked transfer, a closed keep-alive connection). Lost data shows as a gap or a short copy instead.
BENIGN_ERRORS = ("Error parsing Opus packet header", "Input/output error", "End of file",
                 "Error reading HTTP response")


def classify_log_line(level: str, line: str, converting: bool) -> Optional[str]:
    """"lost" for a line meaning that input data was lost, "damaged" for a decoding error, else None.
    ``converting``: the inputs have been probed and the conversion runs."""
    if level not in ("warning", "error", "fatal", "panic"):
        return None
    if any(m in line for m in LOSS_MARKERS):
        markers = LOSS_MARKERS if converting else SEGMENT_LOSS_MARKERS
        return "lost" if any(m in line for m in markers) and not any(m in line for m in NOT_LOSS) else None
    if level != "warning" and not any(m in line for m in BENIGN_ERRORS):
        return "damaged"
    return None


@dataclass
class FFmpegResult:
    log: str  # tail of ffmpeg's log
    issues: List[str] = field(default_factory=list)  # error-level lines: damaged input (concealed)
    data_lost: List[str] = field(default_factory=list)  # warnings/errors matching LOSS_MARKERS
    dup_frames: int = 0  # frames the muxer repeated to keep a constant rate (from the progress report)
    filter_dups: int = 0  # frames the fps filter repeated / dropped (its statistics, if one was used)
    filter_drops: int = 0


def run_ffmpeg(
    args: Sequence[str],
    *,
    duration_s: Optional[float] = None,
    on_progress: Optional[ProgressCallback] = None,
    should_cancel: Optional[CancelCheck] = None,
    stall_timeout_s: Optional[float] = 120.0,
) -> FFmpegResult:
    """Run ffmpeg with machine-readable progress.

    ``args`` are everything after the global options (inputs, filters, outputs).
    Progress is reported as a fraction of ``duration_s`` when given. The process is killed when
    ``should_cancel()`` becomes true (raising CancelledError) or when its output position stops
    advancing for ``stall_timeout_s`` seconds (raising FFmpegError); the final phase (e.g. moving the
    MP4 index to the front) gets a longer grace period because it reports nothing.
    """
    tools = find_toolchain()
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "level+verbose", "-progress", "pipe:1",
           "-nostats", *_compat(args)]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=_POPEN_FLAGS,
    )
    log: Deque[str] = deque(maxlen=200)
    issues: List[str] = []
    lost: List[str] = []
    fps_stats: List[Tuple[int, int, int]] = []  # (frames in, dropped, duplicated) per fps filter instance
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def pump_stderr() -> None:
        converting = False  # "Output #0" is printed once the inputs have been probed
        for raw in proc.stderr:  # type: ignore[union-attr]
            tag = _LEVEL_TAG.search(raw)
            level = tag.group(1) if tag else "info"
            line = raw[:tag.start()] + raw[tag.end():] if tag else raw
            if level in ("verbose", "debug", "trace"):  # read for the fps filter's statistics only
                m = _FPS_STATS.search(line)
                if m:
                    fps_stats.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
                continue
            log.append(line)
            converting = converting or (level == "info" and line.startswith("Output #"))
            kind = classify_log_line(level, line, converting)
            if kind == "lost" and len(lost) < 20:
                lost.append(line.strip())
            elif kind == "damaged" and len(issues) < 20:
                issues.append(line.strip())

    def pump_stdout() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)
        lines.put(None)

    threads = [threading.Thread(target=pump_stderr, daemon=True), threading.Thread(target=pump_stdout, daemon=True)]
    for t in threads:
        t.start()

    # ffmpeg 7+ prints a progress block every 0.5 s even while its input is blocked, so a stall is
    # detected from the reported position/size not changing, not from the absence of output.
    progress: Dict[str, str] = {}
    out_time_s = 0.0
    last_change = time.monotonic()
    stdout_open = True
    try:
        while stdout_open or proc.poll() is None:
            if should_cancel is not None and should_cancel():
                raise CancelledError()
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                line = ""
            if line is None:
                stdout_open = False
                continue
            key, _, value = line.strip().partition("=")
            if key == "dup_frames":
                progress[key] = value
            if key in ("out_time_us", "total_size") and progress.get(key) != value:
                progress[key] = value
                last_change = time.monotonic()
                if key == "out_time_us" and value.lstrip("-").isdigit():
                    out_time_s = int(value) / 1e6
                    if on_progress is not None and duration_s:
                        on_progress(max(0.0, min(1.0, out_time_s / duration_s)))
            if stall_timeout_s:
                limit = stall_timeout_s * (4 if duration_s and out_time_s >= 0.98 * duration_s else 1)
                if time.monotonic() - last_change > limit:
                    raise FFmpegError(f"ffmpeg made no progress for {limit:.0f} s", _tail(log))
        proc.wait()
    except BaseException:
        _kill(proc)
        raise
    finally:
        for t in threads:
            t.join(timeout=2)

    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed (exit code {proc.returncode})", _tail(log))
    if on_progress is not None:
        on_progress(1.0)
    dups = progress.get("dup_frames", "0")
    _frames_in, drops, fps_dups = max(fps_stats, default=(0, 0, 0))  # the instance that saw the frames
    return FFmpegResult(_tail(log, 60), issues, lost, int(dups) if dups.isdigit() else 0, fps_dups, drops)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
