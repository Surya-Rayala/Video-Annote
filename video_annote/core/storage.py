# video_annote/core/storage.py
"""Reading and writing the on-disk layout.

    <root>/config.json            labels + stable color map
    <root>/<session>/session.json videos (+ ingest metadata) and view selections
    <root>/<session>/label.tsv    annotations (the dataset contract; column order is fixed)
    <root>/<session>/video-N.mp4  working copies of the imported videos

All writes are atomic (temp file + rename). Files that cannot be parsed are never silently
replaced: they are copied to a backup first and the caller is told.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import AnnotationRecord, RootConfig, SessionState, VideoItem, clean_text

ROOT_CONFIG_FILENAME = "config.json"
SESSION_META_FILENAME = "session.json"
TSV_FILENAME = "label.tsv"
BACKUP_DIRNAME = ".video-annote-backups"
TRASH_DIRNAME = ".video-annote-trash"
RECOVERY_DIRNAME = ".video-annote-recovered"
SESSION_META_VERSION = 2
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm")

TSV_HEADER = [
    "label", "camid", "step_no", "step_name",
    "start_frame", "end_frame", "total_frames",
    "start_time", "end_time", "total_time",
    "time_source", "audio_source", "confidence", "notes",
]

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class StorageError(RuntimeError):
    """A file could not be read or written; ``backup`` points at a preserved copy if one was made."""

    def __init__(self, message: str, backup: Optional[str] = None, raw_hash: Optional[str] = None) -> None:
        super().__init__(message)
        self.backup = backup
        self.raw_hash = raw_hash  # content hash of the file as read (it was copied aside: not "changed")


class SessionMovedError(StorageError):
    """The session folder was replaced, moved or deleted since it was loaded: nothing was written
    (see save_recovery_copy)."""


# -----------------------------
# Low-level helpers
# -----------------------------

def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


# mkstemp creates 0600 files; written files should get the usual permissions instead (read once at
# import, while the process is still single-threaded, because reading the umask briefly changes it).
_DEFAULT_FILE_MODE = 0o666 & ~_umask()


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)  # keep the existing file's permissions
    except OSError:
        mode = _DEFAULT_FILE_MODE
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _json_text(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    atomic_write_text(path, _json_text(payload))


def read_json(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        data = json.loads(f.read())  # bytes: UTF-8 with or without BOM, or UTF-16/32 (Windows editors)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    return data


def timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _content_hash(path: str) -> Optional[str]:
    """Hash of a file's content, None if it does not exist. (Contents, not timestamps, tell whether
    a file changed: FAT32 and HFS+ volumes have 1-2 s timestamps.)"""
    try:
        with open(path, "rb") as f:
            return _hash_bytes(f.read())
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"  # never equal to a known hash: the caller then tries (and fails) to back it up


# signature of a file that was loaded with problems but could not be copied aside then: equal to no
# content hash (nor to _content_hash()'s "unreadable"), so a save backs it up first, or refuses
_NOT_KEPT = "not kept"


def _try_backup(path: str) -> Optional[str]:
    """backup_file() that never raises: a failed backup must not hide the original problem."""
    try:
        return backup_file(path)
    except OSError:
        return None


def backup_file(path: str) -> Optional[str]:
    """Copy ``path`` into a hidden backup folder next to it; returns the backup path."""
    if not os.path.isfile(path):
        return None
    folder = os.path.join(os.path.dirname(path), BACKUP_DIRNAME)
    os.makedirs(folder, exist_ok=True)
    name, ext = os.path.splitext(os.path.basename(path))
    dst = os.path.join(folder, f"{name}.{timestamp()}{ext}")
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(folder, f"{name}.{timestamp()}-{n}{ext}")
        n += 1
    shutil.copy2(path, dst)
    return dst


# -----------------------------
# Names and folders
# -----------------------------

def validate_session_label(label: str) -> Optional[str]:
    """Return an error message if ``label`` cannot be used as a session folder name, else None."""
    if not label or not label.strip():
        return "Please enter a session label."
    if label != label.strip():
        return "Session label must not start or end with spaces."
    if label.startswith("."):
        return "Session label must not start with a dot."
    if label.endswith("."):
        return "Session label must not end with a dot."
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', label):
        return 'Session label must not contain any of < > : " / \\ | ? * or control characters.'
    if label.split(".")[0].lower() in _WINDOWS_RESERVED:
        return f"'{label}' is a reserved name on Windows."
    if len(label) > 120:
        return "Session label is too long (max 120 characters)."
    return None


def session_dir(root_dir: str, label: str) -> str:
    return os.path.join(root_dir, label)


def session_exists(root_dir: str, label: str) -> Tuple[bool, str]:
    p = session_dir(root_dir, label)
    return os.path.exists(p), p


def move_to_trash(path: str, root_dir: str) -> str:
    """Move a session folder out of the way without deleting anything; returns its new location."""
    trash = os.path.join(root_dir, TRASH_DIRNAME)
    os.makedirs(trash, exist_ok=True)
    dst = os.path.join(trash, f"{os.path.basename(os.path.normpath(path))}.{timestamp()}")
    n = 1
    while os.path.exists(dst):
        dst = f"{dst}-{n}"
        n += 1
    shutil.move(path, dst)
    return dst


def list_sessions(root_dir: str) -> List[str]:
    """Session folder names in ``root_dir`` (folders with session.json / label.tsv / video-N files)."""
    if not root_dir or not os.path.isdir(root_dir):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(root_dir)):
        p = os.path.join(root_dir, name)
        if name.startswith(".") or not os.path.isdir(p):
            continue
        if os.path.exists(os.path.join(p, SESSION_META_FILENAME)) or os.path.exists(os.path.join(p, TSV_FILENAME)):
            out.append(name)
            continue
        try:
            if any(fn.lower().startswith("video-") and fn.lower().endswith(VIDEO_EXTS) for fn in os.listdir(p)):
                out.append(name)
        except OSError:
            pass
    return out


# -----------------------------
# Root config
# -----------------------------

def root_config_path(root_dir: str) -> str:
    return os.path.join(root_dir, ROOT_CONFIG_FILENAME)


def load_root_config(root_dir: str, backup: bool = True) -> Optional[RootConfig]:
    """Load <root>/config.json. Returns None when it does not exist. If it exists but cannot be
    read or parsed, StorageError is raised (after backing it up when ``backup``), so it is never
    silently replaced by an empty configuration."""
    path = root_config_path(root_dir)
    if not root_dir or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:  # e.g. no permission: then usually no copy can be made either
        saved = _try_backup(path) if backup else None
        raise StorageError(f"Could not read {path}: {e}", backup=saved) from e
    try:
        data = json.loads(raw)  # bytes: UTF-8 with or without BOM, or UTF-16/32 (Windows editors)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value is not an object")
        cfg = RootConfig.from_dict(data)
    except (ValueError, TypeError, AttributeError, OverflowError) as e:
        saved = _try_backup(path) if backup else None
        raise StorageError(f"Could not parse {path}: {e}", backup=saved, raw_hash=_hash_bytes(raw)) from e
    cfg.root_dir = root_dir
    cfg.disk_hash = _hash_bytes(raw)
    cfg.saved_state = cfg.to_dict()  # nothing is written back unless the labels or colors change
    if cfg.problems and backup:  # entries that were left out: keep the file as it was
        saved = _try_backup(path)
        cfg.problems.append(f"a copy of the file was saved to {saved}" if saved else "no copy could be made")
        if not saved:  # then the next save must make one first (or refuse)
            cfg.disk_hash = _NOT_KEPT
    return cfg


def save_root_config(cfg: RootConfig) -> Optional[str]:
    """Write config.json if the labels or colors changed since it was read or last written. If the
    file was changed elsewhere meanwhile (another window or program): when this configuration only
    gave labels colors, the other version is kept and those colors are added to it (``cfg`` takes its
    labels); otherwise the other version is backed up first and its backup path returned, so the
    caller can tell the user."""
    if not cfg.root_dir:
        raise ValueError("RootConfig.root_dir is required")
    if not os.path.isdir(cfg.root_dir):
        raise StorageError(f"The data root {cfg.root_dir} no longer exists (renamed or moved?), so the label list "
                           "was not saved. Select the data root again.")
    data = cfg.to_dict()
    if cfg.saved_state is not None and data == cfg.saved_state:
        return None
    path = root_config_path(cfg.root_dir)
    current = _content_hash(path)
    kept = None
    if current is not None and current != cfg.disk_hash:  # changed elsewhere since it was read
        theirs = None
        if cfg.saved_state is not None and data["skills"] == cfg.saved_state["skills"]:
            try:  # this window only gave labels colors: keep the other version, plus these colors
                theirs = load_root_config(cfg.root_dir, backup=False)
            except StorageError:
                theirs = None
            if theirs is not None and theirs.problems:  # entries this version cannot read: back it up
                theirs = None
        if theirs is not None:
            used = set(theirs.skill_color_map.values())
            for number, index in cfg.skill_color_map.items():
                if number not in theirs.skill_color_map and index not in used:
                    theirs.skill_color_map[number] = index
                    used.add(index)
            cfg.skills = theirs.skills
            cfg.skill_color_map.clear()  # in place: the label list widget draws from this dict
            cfg.skill_color_map.update(theirs.skill_color_map)
            data = cfg.to_dict()
        else:
            kept = backup_file(path)
    text = _json_text(data)
    atomic_write_text(path, text)
    cfg.disk_hash, cfg.saved_state = _hash_bytes(text.encode("utf-8")), data
    return kept


# -----------------------------
# Session metadata
# -----------------------------

def session_meta_path(session_dir_path: str) -> str:
    return os.path.join(session_dir_path, SESSION_META_FILENAME)


def tsv_path(session_dir_path: str) -> str:
    return os.path.join(session_dir_path, TSV_FILENAME)


def session_meta_text(state: SessionState) -> str:
    return _json_text({
        "label": state.label,
        "videos": [v.to_dict() for v in state.videos],
        "view": {
            "active_view_ids": list(state.active_view_ids),
            "time_source_id": state.time_source_id,
            "audio_source_id": state.audio_source_id,
        },
        "meta_version": SESSION_META_VERSION,
    })


def save_session_meta(state: SessionState) -> None:
    assert state.session_dir and state.label
    atomic_write_text(session_meta_path(state.session_dir), session_meta_text(state))


def _infer_videos(session_dir_path: str) -> List[VideoItem]:
    files = sorted(os.listdir(session_dir_path)) if os.path.isdir(session_dir_path) else []
    vids = [f for f in files if f.lower().startswith("video-") and f.lower().endswith(VIDEO_EXTS)]
    # natural order: video-2 before video-10
    vids.sort(key=lambda f: (int(m.group(1)) if (m := re.match(r"video-(\d+)", f.lower())) else 1 << 30, f))
    return [VideoItem(video_id=os.path.splitext(f)[0], filename=f, fps=0.0) for f in vids]  # rate: measured later


@dataclass
class LoadReport:
    """Problems found while loading a session; nothing in here was silently discarded."""
    warnings: List[str] = field(default_factory=list)
    skipped_rows: List[Tuple[int, str]] = field(default_factory=list)  # (line number, reason)
    backups: List[str] = field(default_factory=list)


def _folder_id(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def load_session_state(root_dir: str, label: str, backup: bool = True) -> Tuple[SessionState, LoadReport]:
    """Load a session. Unreadable files are reported (and, with ``backup``, copied aside before the
    caller's next save could replace them). Read-only tools such as validate pass backup=False."""
    sdir = session_dir(root_dir, label)
    report = LoadReport()
    state = SessionState(root_dir=root_dir, label=label, session_dir=sdir)
    state.folder_id = _folder_id(sdir)

    meta: Optional[Dict[str, Any]] = None
    meta_unreadable = False
    meta_path = session_meta_path(sdir)

    def keep_copy(path: str) -> Optional[str]:
        saved = _try_backup(path) if backup else None
        if saved and saved not in report.backups:
            report.backups.append(saved)
        return saved

    try:
        raw = _read_bytes(meta_path)
    except OSError as e:  # e.g. permissions: inferring its videos would lose their details on the next save
        raise StorageError(f"Could not read {meta_path} ({e}). Fix it (for example its permissions) and open the "
                           "session again.") from e
    state.disk_signatures[SESSION_META_FILENAME] = _hash_bytes(raw) if raw is not None else None
    try:
        if raw is not None:
            meta = json.loads(raw)  # bytes: UTF-8 with or without BOM, or UTF-16/32
            if not isinstance(meta, dict):
                raise ValueError("top-level JSON value is not an object")
    except ValueError as e:
        meta, meta_unreadable = None, True
        saved = keep_copy(meta_path)
        if backup and not saved:
            state.disk_signatures[SESSION_META_FILENAME] = _NOT_KEPT
        report.warnings.append(
            f"session.json could not be read ({e}); videos were inferred from file names"
            + (f" (a copy of the file was saved to {saved})." if saved else "."))

    if meta and isinstance(meta.get("videos"), list):
        bad: List[str] = []
        for raw_item in meta["videos"]:
            try:
                state.videos.append(VideoItem.from_dict(raw_item))
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as e:
                bad.append(f"{str(raw_item)[:60]} ({type(e).__name__}: {e})")
        if bad:  # never drop entries silently: the next save would remove them from the file
            saved = keep_copy(meta_path)
            if backup and not saved:
                state.disk_signatures[SESSION_META_FILENAME] = _NOT_KEPT
            inferred = not state.videos
            if inferred:
                state.videos = _infer_videos(sdir)
            report.warnings.append(
                f"{len(bad)} video entr{'y' if len(bad) == 1 else 'ies'} of session.json could not be read and "
                + ("the videos were inferred from file names" if inferred else "were left out")
                + f": {'; '.join(bad[:3])}" + (f" (a copy of the file was saved to {saved})." if saved else "."))
        view = meta.get("view") if isinstance(meta.get("view"), dict) else {}
        state.active_view_ids = [str(v) for v in view.get("active_view_ids") or [] if isinstance(v, str)]
        state.time_source_id = view.get("time_source_id") if isinstance(view.get("time_source_id"), str) else None
        state.audio_source_id = view.get("audio_source_id") if isinstance(view.get("audio_source_id"), str) else None
    else:
        state.videos = _infer_videos(sdir)

    for v in state.videos:
        if not os.path.exists(os.path.join(sdir, v.filename)):
            report.warnings.append(f"{v.video_id}: file {v.filename} is missing from the session folder.")

    # 0.1.x escaped only newlines in notes; 0.2+ (meta_version 2) escapes \\, \t, \r and \n. A
    # session.json that exists but cannot be parsed tells nothing about the version: use the current rules
    version = meta.get("meta_version") if meta else None
    legacy = not meta_unreadable and not (isinstance(version, int) and version >= 2)
    path = tsv_path(sdir)
    raw = _read_bytes(path)
    state.disk_signatures[TSV_FILENAME] = _hash_bytes(raw) if raw is not None else None
    if raw is not None:
        text = _decode_tsv(raw, path)
        state.annotations, skipped = parse_annotations_tsv(text, legacy_notes=legacy)
        extra = _extra_tsv_columns(text)
        saved = keep_copy(path) if skipped or extra else None
        if (skipped or extra) and backup and not saved:  # the next save would drop them: it must copy it first
            state.disk_signatures[TSV_FILENAME] = _NOT_KEPT
        report.skipped_rows = skipped
        if extra:
            report.warnings.append(
                f"label.tsv has columns that video-annote does not keep ({', '.join(extra)}); they are left "
                "out when the session is saved" + (f" (a copy of the file was saved to {saved})." if saved else "."))
    state.ensure_default_sources()
    return state, report


def _check_folder(state: SessionState) -> None:
    """Refuse to write into a session folder that is not the one this state was loaded from."""
    sdir = state.session_dir or ""
    if state.folder_id is None:  # a session being created (or a file system without identities)
        os.makedirs(sdir, exist_ok=True)
        state.folder_id = _folder_id(sdir)
        return
    current = _folder_id(sdir)
    if current is None:
        raise SessionMovedError(f"The session folder {sdir} no longer exists (renamed, moved or deleted "
                                "while it was open).")
    if current != state.folder_id:
        # another folder now has this path. Network drives can report new identities after a remount,
        # so it only counts as replaced if its session.json is not the one this state knows
        if _content_hash(session_meta_path(sdir)) != state.disk_signatures.get(SESSION_META_FILENAME):
            raise SessionMovedError(f"The session folder {sdir} was replaced while it was open (for example "
                                    "by an import that replaced this session).")
        state.folder_id = current


def persist_session_state(state: SessionState) -> List[str]:
    """Write label.tsv and session.json atomically.

    If either file changed since this state last read or wrote it (the same session open in another
    window, or edited by another program), that version is backed up before it is replaced; the
    backup paths are returned so the caller can tell the user. If the session folder itself was
    replaced, moved or deleted, SessionMovedError is raised and nothing is written
    (see save_recovery_copy)."""
    if not state.session_dir or not state.label:
        raise ValueError("SessionState must have session_dir and label to persist")
    _check_folder(state)
    backups: List[str] = []
    tsv, meta = tsv_path(state.session_dir), session_meta_path(state.session_dir)
    for name, path in ((TSV_FILENAME, tsv), (SESSION_META_FILENAME, meta)):
        if name in state.disk_signatures and _content_hash(path) != state.disk_signatures[name]:
            saved = backup_file(path)  # raises (and nothing is written) if it cannot be kept
            if saved:
                backups.append(saved)
    # session.json's meta_version declares how label.tsv's notes are encoded, so the two must change
    # together: label.tsv first, and back to its previous content if session.json cannot be written
    previous = _read_bytes(tsv)
    known = state.disk_signatures.get(TSV_FILENAME)
    text = annotations_tsv_text(state.annotations)
    atomic_write_text(tsv, text)
    state.disk_signatures[TSV_FILENAME] = _hash_bytes(text.encode("utf-8"))
    try:
        text = session_meta_text(state)
        atomic_write_text(meta, text)
        state.disk_signatures[SESSION_META_FILENAME] = _hash_bytes(text.encode("utf-8"))
    except OSError:
        if previous is not None:
            try:
                atomic_write_text(tsv, previous.decode("utf-8"))
                # as before this attempt: a version written elsewhere stays "changed elsewhere", so the
                # next save backs it up again and says so
                state.disk_signatures[TSV_FILENAME] = known
            except (OSError, UnicodeDecodeError):
                pass
        raise
    return backups


def save_recovery_copy(state: SessionState, folder: Optional[str] = None) -> str:
    """Write the session's label.tsv and session.json to ``folder`` (default: a new folder under
    <root>/.video-annote-recovered), e.g. when its own folder was replaced while it was open.
    Returns the folder used: a new one if ``folder``'s parent is gone (the data root was renamed
    or moved meanwhile, and must not be recreated)."""
    if folder is not None and not os.path.isdir(os.path.dirname(os.path.abspath(folder))):
        folder = None
    if folder is None:
        root = state.root_dir or os.path.dirname(state.session_dir or ".")
        base = (os.path.join(root, RECOVERY_DIRNAME) if os.path.isdir(root)  # never recreate a moved root
                else os.path.join(os.path.expanduser("~"), "video-annote-recovered"))
        folder = os.path.join(base, f"{state.label}.{timestamp()}")
        n = 1
        while os.path.exists(folder):
            folder = os.path.join(base, f"{state.label}.{timestamp()}-{n}")
            n += 1
    os.makedirs(folder, exist_ok=True)
    atomic_write_text(os.path.join(folder, TSV_FILENAME), annotations_tsv_text(state.annotations))
    atomic_write_text(os.path.join(folder, SESSION_META_FILENAME), session_meta_text(state))
    return folder


def validate_importable_session(root_dir: str, label: str) -> Tuple[bool, str]:
    if not root_dir or not os.path.isdir(root_dir):
        return False, "Root directory is not set or does not exist."
    sdir = session_dir(root_dir, label)
    if not os.path.isdir(sdir):
        return False, f"Session folder does not exist: {sdir}"
    has_meta = os.path.exists(session_meta_path(sdir))
    if not has_meta and not _infer_videos(sdir):
        return False, "No videos found (expected session.json or files named like video-1.mp4)."
    return True, "OK"


# -----------------------------
# label.tsv
# -----------------------------

_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESCAPES = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


def escape_notes(text: str) -> str:
    """Lossless single-line encoding of free text (backslash, tab, newline, CR)."""
    return "".join(_ESCAPES.get(ch, ch) for ch in str(text or ""))


def unescape_notes(text: str, legacy: bool = False) -> str:
    """Inverse of escape_notes. ``legacy`` decodes files written by 0.1.x, which escaped only
    newlines (backslashes were written raw), exactly as 0.1.x read them back."""
    if legacy:
        return str(text or "").replace("\\n", "\n")
    out: List[str] = []
    i, s = 0, str(text or "")
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] in _UNESCAPES:
            out.append(_UNESCAPES[s[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def annotation_to_row(rec: AnnotationRecord) -> List[str]:
    return [
        clean_text(rec.label), clean_text(rec.camid), str(int(rec.step_no)), clean_text(rec.step_name),
        str(int(rec.start_frame)), str(int(rec.end_frame)), str(int(rec.total_frames)),
        f"{float(rec.start_time):.3f}", f"{float(rec.end_time):.3f}", f"{float(rec.total_time):.3f}",
        clean_text(rec.time_source), clean_text(rec.audio_source), str(int(rec.confidence)),
        escape_notes(rec.notes),
    ]


def annotations_tsv_text(annotations: List[AnnotationRecord]) -> str:
    lines = ["\t".join(TSV_HEADER)] + ["\t".join(annotation_to_row(r)) for r in annotations]
    return "\n".join(lines) + "\n"


def save_annotations_tsv(session_dir_path: str, annotations: List[AnnotationRecord]) -> str:
    path = tsv_path(session_dir_path)
    atomic_write_text(path, annotations_tsv_text(annotations))
    return path


# Rows end in \n, \r\n or a bare \r (classic Mac exports); fields never contain a raw CR or LF (notes
# escape them). str.splitlines() is not used: it would also break rows at U+2028, U+0085, \x0c ...
# that can legitimately appear inside pasted notes.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
REQUIRED_COLUMNS = ("step_no", "start_time", "end_time", "time_source")


def _decode_tsv(raw: bytes, path: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise StorageError(f"{path} is not UTF-8 text (was it re-saved by a spreadsheet program?): {e}") from e


def _first_line(text: str) -> Tuple[int, List[str]]:
    """Index and fields of the first non-blank line."""
    lines = _LINE_BREAK.split(text)
    for i, line in enumerate(lines):
        if line.strip():
            return i, line.split("\t")
    return len(lines), []


def _extra_tsv_columns(text: str) -> List[str]:
    """Columns of label.tsv that are not part of the format (e.g. added by a data team), including
    unnamed or repeated ones that hold values: the next save would leave them out."""
    index, first = _first_line(text)
    first = [c.strip() for c in first]
    if "label" not in first or "step_no" not in first:
        return []
    extra = [c for c in first if c and c not in TSV_HEADER]
    odd = [i for i, c in enumerate(first) if not c or first.index(c) != i]  # unnamed, or a repeat
    if odd:
        rows = [line.split("\t") for line in _LINE_BREAK.split(text)[index + 1:] if line.strip()]
        for i in odd:
            if any(len(r) > i and r[i].strip() for r in rows):
                extra.append(f"'{first[i]}' (column {i + 1}, repeated)" if first[i] else f"unnamed column {i + 1}")
    return extra


def _finite(key: str, text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{key} is {text!r}, not a finite number")
    return value


def _row_to_record(d: Dict[str, str], legacy_notes: bool = False) -> AnnotationRecord:
    def i(key: str, default: int = 0) -> int:
        v = (d.get(key) or "").strip()
        if not v:
            return default
        value = _finite(key, v)
        if not value.is_integer():
            raise ValueError(f"{key} is {v!r}, not a whole number")
        return int(value)

    def f(key: str) -> float:
        v = (d.get(key) or "").strip()
        return _finite(key, v) if v else 0.0

    return AnnotationRecord(
        label=d.get("label", ""), camid=d.get("camid", ""), step_no=i("step_no"),
        step_name=d.get("step_name", ""), start_frame=i("start_frame"), end_frame=i("end_frame"),
        total_frames=i("total_frames"), start_time=f("start_time"), end_time=f("end_time"),
        total_time=f("total_time"), time_source=d.get("time_source", ""),
        audio_source=d.get("audio_source", ""), confidence=i("confidence", 5),
        notes=unescape_notes(d.get("notes", ""), legacy=legacy_notes),
    )


def parse_annotations_tsv(text: str, legacy_notes: bool = False
                          ) -> Tuple[List[AnnotationRecord], List[Tuple[int, str]]]:
    """Return (records, skipped) where skipped lists (line number, reason) for rows that cannot be
    read as written: wrong number of fields, a required value missing, or a malformed number. Those
    rows are reported, never repaired by guessing. Columns are matched by header name, so reordered
    or extra columns are tolerated."""
    lines = _LINE_BREAK.split(text)
    first_index, first = _first_line(text)
    if not first:
        return [], []
    has_header = "label" in first and "step_no" in first
    columns = [c.strip() for c in first] if has_header else TSV_HEADER
    required = [c for c in REQUIRED_COLUMNS if c in columns]
    records: List[AnnotationRecord] = []
    skipped: List[Tuple[int, str]] = []
    start = first_index + 1 if has_header else first_index
    for line_no, line in enumerate(lines[start:], start=start + 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) > len(columns):
            skipped.append((line_no, f"{len(parts)} fields but {len(columns)} columns (a tab inside a field?)"))
            continue
        row: Dict[str, str] = {}
        for name, value in zip(columns, parts + [""] * (len(columns) - len(parts))):
            row.setdefault(name, value)  # a repeated column name: the first one counts (see _extra_tsv_columns)
        missing = [c for c in required if not row[c].strip()]
        if missing:
            skipped.append((line_no, f"no {', '.join(missing)}"))
            continue
        try:
            records.append(_row_to_record(row, legacy_notes))
        except (ValueError, TypeError, OverflowError) as e:
            skipped.append((line_no, str(e)))
    return records, skipped


def load_annotations_tsv(session_dir_path: str, legacy_notes: bool = False
                         ) -> Tuple[List[AnnotationRecord], List[Tuple[int, str]]]:
    """Read label.tsv (see parse_annotations_tsv); a missing file gives no records."""
    path = tsv_path(session_dir_path)
    raw = _read_bytes(path)
    if raw is None:
        return [], []
    return parse_annotations_tsv(_decode_tsv(raw, path), legacy_notes)
