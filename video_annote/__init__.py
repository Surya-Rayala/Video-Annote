# video_annote/__init__.py
'''
video_annote/
    __init__.py
    __main__.py

    app.py                 # QApplication + boot + root selection
    main_window.py         # QMainWindow layout + wiring

    domain.py              # dataclasses: SkillStep, VideoItem, AnnotationRecord, SessionState
    persistence.py         # load/save root config.json, load/save TSV, session metadata
    media_import.py        # add video (local/url), validation, file copy/move, ffmpeg helpers
    timeutils.py           # fps helpers, ms<->time, recompute fields, lane stacking helpers

    widgets/
      video_grid.py        # N video widgets + players + master/slave sync
      range_slider.py      # improved slider overlay support (multi-color masks)
      skill_timeline.py    # skills bar track view with playhead + block click + edit handles
      annotations_table.py # table + context menu + safe edit + notes dialog
      skills_panel.py      # skills list + color squares + add/delete/replace prompt
      checkable_combo.py   # combo box with checkable items + multi-select support

    dialogs/
      create_session.py    # Create session + add video + batch confirm
      import_session.py    # Import existing session dialog
      
'''

from __future__ import annotations

__all__ = ["__version__", "run_app"]

__version__ = "0.1.0"

from .app import run_app