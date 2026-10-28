# video_annote/app.py
"""GUI bootstrap: logging, a non-fatal exception hook, the QApplication and the main window."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from . import __version__

log = logging.getLogger("video_annote")


def _log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "video-annote"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "video-annote" / "logs"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "video-annote"


def _setup_logging() -> Optional[Path]:
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "video-annote.log"
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return path


def _install_excepthook(log_path: Optional[Path]) -> None:
    """PyQt5 aborts the process on an exception escaping a slot unless sys.excepthook is replaced.
    Log it and tell the user instead; the autosaved session is unaffected."""

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Unhandled exception:\n%s", text)
        sys.__stderr__.write(text)
        try:
            from PyQt5.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox(QMessageBox.Critical, "Unexpected error",
                                  f"An unexpected error occurred: {exc}\n\nYour work is autosaved."
                                  + (f"\nDetails were written to {log_path}." if log_path else ""))
                box.setDetailedText(text)
                box.exec_()
        except Exception:  # never let the hook itself crash the app
            pass

    sys.excepthook = hook
    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)


def run_app(root_dir: Optional[str] = None) -> int:
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QGuiApplication
    from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox

    from .media.ffmpeg import FFmpegNotFoundError, ffmpeg_release, ffmpeg_version, find_toolchain
    from .ui.common import app_settings
    from .ui.main_window import MainWindow

    log_path = _setup_logging()
    _install_excepthook(log_path)
    log.info("video-annote %s starting", __version__)

    if QApplication.instance() is None:
        # sharp, correctly sized text on scaled displays (Windows at 125/150 %, Linux); macOS scales by itself
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        try:
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        except AttributeError:  # Qt before 5.14
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("video-annote")
    app.setApplicationVersion(__version__)

    win = MainWindow(root_dir=root_dir)
    if not win.root_dir:  # the data root used last time
        last = app_settings().value("last_root", "", str)
        if last and os.path.isdir(last):
            win.set_root_dir(last)
    win.show()

    try:
        tools = find_toolchain()
        log.info("ffmpeg: %s (%s), ffprobe: %s", tools.ffmpeg, ffmpeg_version(), tools.ffprobe)
        release = ffmpeg_release()
        if release is not None and release < (5, 1):
            QMessageBox.warning(win, "Old ffmpeg",
                                f"{ffmpeg_version()} is older than the versions video-annote is tested with "
                                "(6.1 to 9.0). If importing or exporting fails, install a newer ffmpeg "
                                "(conda-forge, Homebrew or a static build).")
    except FFmpegNotFoundError as e:
        QMessageBox.warning(win, "ffmpeg not found",
                            f"{e}\n\nYou can still open existing sessions, but importing videos and exporting "
                            "clips need ffmpeg.")

    if not win.root_dir:
        QMessageBox.information(win, "Select Data Root",
                                "Please choose a data root directory to store sessions and config.json.")
        chosen = QFileDialog.getExistingDirectory(win, "Select Data Root")
        if chosen:
            win.set_root_dir(chosen)
    return app.exec_()
