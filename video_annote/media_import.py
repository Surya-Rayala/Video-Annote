# video_annote/media_import.py
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse


# Allowed local extensions (strict)
ALLOWED_VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm",
}
ALLOWED_URL_EXTS = ALLOWED_VIDEO_EXTS | {".m3u8"}


def is_probably_url(s: str) -> bool:
    try:
        p = urlparse(s.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def ext_lower(path_or_url: str) -> str:
    base = path_or_url.strip().split("?")[0].split("#")[0]
    _, ext = os.path.splitext(base)
    return ext.lower().strip()


def validate_local_video_path(path: str) -> Tuple[bool, str]:
    if not path:
        return (False, "No file selected.")
    if not os.path.exists(path):
        return (False, f"File does not exist: {path}")
    if not os.path.isfile(path):
        return (False, f"Not a file: {path}")
    ext = ext_lower(path)
    if ext not in ALLOWED_VIDEO_EXTS:
        return (False, f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_VIDEO_EXTS)}")
    return (True, "OK")


def validate_video_url(url: str) -> Tuple[bool, str]:
    if not url or not is_probably_url(url):
        return (False, "Invalid URL.")
    ext = ext_lower(url)
    if ext and ext not in ALLOWED_URL_EXTS:
        return (False, f"Unsupported URL type '{ext}'. Allowed: {sorted(ALLOWED_URL_EXTS)}")
    # ext may be empty for some CDNs; allow if it's http(s)
    return (True, "OK")


def next_video_id(existing_count: int) -> str:
    return f"video-{existing_count + 1}"


def video_filename_for_id(video_id: str, ext: str = ".mp4") -> str:
    # enforce "video-N.mp4"
    if not video_id.lower().startswith("video-"):
        video_id = "video-" + str(video_id)
    return f"{video_id}{ext}"


def ensure_session_dir(root_dir: str, label: str) -> str:
    if not root_dir:
        raise ValueError("root_dir is required")
    if not label:
        raise ValueError("label is required")
    session_dir = os.path.join(root_dir, label)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def safe_remove(path: str) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# -----------------------------
# ffmpeg helpers
# -----------------------------

def find_ffmpeg() -> str:
    # rely on PATH; allow override via env
    return os.environ.get("FFMPEG_BIN", "ffmpeg")


def find_ffprobe() -> str:
    return os.environ.get("FFPROBE_BIN", "ffprobe")


def run_cmd(cmd: List[str]) -> Tuple[int, str]:
    """
    Runs a command and returns (returncode, combined_output).
    """
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    try:
        text = out.decode("utf-8", errors="ignore")
    except Exception:
        text = str(out)
    return proc.returncode, text


def ffmpeg_remux(src: str, dst: str) -> None:
    """
    For m3u8 or downloadable URL (or file), remux into mp4 container.
    Uses stream copy when possible.
    """
    if os.path.exists(dst):
        os.remove(dst)
    cmd = [
        find_ffmpeg(),
        "-y",
        "-i", src,
        "-c", "copy",
        dst,
    ]
    code, out = run_cmd(cmd)
    if code != 0:
        raise RuntimeError(out.strip() or "ffmpeg failed")


def ffmpeg_transcode_to_mp4(src: str, dst: str) -> None:
    """Transcode to a highly-compatible H.264/AAC MP4 without changing resolution.

    - Video: H.264 (libx264), yuv420p pixel format (broad compatibility)
    - Audio: AAC (or dropped if missing)
    - Container: MP4 with faststart for better playback

    Notes:
      - No scaling filter is applied, so the input resolution is preserved.
      - We map the first video stream and (optionally) the first audio stream.
    """
    if os.path.exists(dst):
        os.remove(dst)

    cmd = [
        find_ffmpeg(),
        "-y",
        "-i", src,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        dst,
    ]
    code, out = run_cmd(cmd)
    if code != 0:
        raise RuntimeError(out.strip() or "ffmpeg transcode failed")


def ffprobe_get_duration_fps(path: str) -> Tuple[int, float]:
    """
    Best-effort duration(ms) and fps for a local file.
    Returns (0, 30.0) if ffprobe unavailable or parse fails.
    """
    try:
        cmd = [
            find_ffprobe(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate:format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        code, out = run_cmd(cmd)
        if code != 0:
            return (0, 30.0)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # ffprobe output order can vary; try robust parse
        duration_s: Optional[float] = None
        fps: Optional[float] = None

        def parse_rate(s: str) -> Optional[float]:
            # "30000/1001"
            if "/" in s:
                a, b = s.split("/", 1)
                try:
                    return float(a) / float(b)
                except Exception:
                    return None
            try:
                return float(s)
            except Exception:
                return None

        for l in lines:
            # duration line is usually a float like "12.345000"
            if re.fullmatch(r"\d+(\.\d+)?", l) and duration_s is None:
                try:
                    duration_s = float(l)
                    continue
                except Exception:
                    pass
            if "/" in l and fps is None:
                fr = parse_rate(l)
                if fr and fr > 0:
                    fps = fr

        dur_ms = int(round((duration_s or 0.0) * 1000.0))
        return (dur_ms, float(fps or 30.0))
    except Exception:
        return (0, 30.0)


# -----------------------------
# Import operations
# -----------------------------

@dataclass
class ImportedVideoInfo:
    video_id: str
    filename: str
    source_type: str  # "local" or "url"
    source: str
    duration_ms: int = 0
    fps: float = 30.0


def import_local_video_into_session(
    session_dir: str,
    video_id: str,
    local_path: str,
    copy_instead_of_move: bool = True,
    force_mp4_name: bool = True,
) -> ImportedVideoInfo:
    """
    Validates and copies/moves a local file into the session folder.

    - If force_mp4_name: stored as video-N + original ext? (we keep ext)
      but default behavior is: keep original ext? You requested strict "video-n naming match as video-n".
      We'll store as video-N.<ext> by default to preserve container; later we can optionally remux to .mp4.
    """
    ok, msg = validate_local_video_path(local_path)
    if not ok:
        raise ValueError(msg)

    # Always store as a re-encoded, highly-compatible MP4 to avoid "black video" issues
    # from uncommon codecs/profiles that Qt backends sometimes fail to decode.
    dst_name = f"{video_id}.mp4"
    dst_path = os.path.join(session_dir, dst_name)

    # Replace existing
    if os.path.exists(dst_path):
        os.remove(dst_path)

    # Transcode into the session folder (preserves resolution; improves compatibility)
    ffmpeg_transcode_to_mp4(local_path, dst_path)

    # If caller requested move semantics, remove the original after a successful transcode.
    if not copy_instead_of_move:
        try:
            os.remove(local_path)
        except Exception:
            pass

    dur_ms, fps = ffprobe_get_duration_fps(dst_path)
    return ImportedVideoInfo(
        video_id=video_id,
        filename=dst_name,
        source_type="local",
        source=local_path,
        duration_ms=dur_ms,
        fps=fps,
    )


def import_url_video_into_session(
    session_dir: str,
    video_id: str,
    url: str,
    force_mp4_output: bool = True,
) -> ImportedVideoInfo:
    """
    Imports a URL into the session folder using ffmpeg.

    - Supports m3u8 and direct downloadable URLs.
    - Writes to video-N.mp4 (by default).
    """
    ok, msg = validate_video_url(url)
    if not ok:
        raise ValueError(msg)

    # Always store as mp4 when coming from URL
    dst_name = f"{video_id}.mp4" if force_mp4_output else f"{video_id}{ext_lower(url) or '.mp4'}"
    dst_path = os.path.join(session_dir, dst_name)

    # Always transcode for maximum decoder compatibility.
    ffmpeg_transcode_to_mp4(url, dst_path)
    dur_ms, fps = ffprobe_get_duration_fps(dst_path)

    return ImportedVideoInfo(
        video_id=video_id,
        filename=dst_name,
        source_type="url",
        source=url,
        duration_ms=dur_ms,
        fps=fps,
    )