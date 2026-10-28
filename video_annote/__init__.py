"""video-annote: multi-video annotation tool for building labeled, audio-synchronized clip datasets.

Package layout:
    core/    data model, label colors, time/frame math, on-disk storage (no Qt, no ffmpeg)
    media/   ffmpeg/ffprobe integration: probing, audio analysis, import, clip export (no Qt)
    ui/      PyQt5 widgets, dialogs and the main window
    sessions.py  session creation/validation shared by the GUI and the CLI
    cli.py       `video-annote` command line;  app.py  GUI bootstrap
"""

__version__ = "0.2.0"
__all__ = ["__version__", "run_app"]


def run_app(root_dir=None) -> int:
    """Start the GUI (imported lazily so the CLI and tests never load Qt)."""
    from .app import run_app as _run_app

    return _run_app(root_dir)
