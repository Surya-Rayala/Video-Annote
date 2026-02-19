# video_annote/app.py
from __future__ import annotations

import sys
from typing import Optional

from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox

from .main_window import MainWindow


def choose_root_dir(parent=None) -> Optional[str]:
    d = QFileDialog.getExistingDirectory(parent, "Select Data Root")
    return d or None


def run_app(root_dir: Optional[str] = None) -> int:
    app = QApplication(sys.argv)

    win = MainWindow(root_dir=root_dir)
    win.show()

    # If root not set, prompt once (non-blocking for main window usage)
    if not root_dir and not win.root_dir:
        QMessageBox.information(
            win,
            "Select Data Root",
            "Please choose a data root directory to store sessions and the config.json.",
        )
        d = choose_root_dir(win)
        if d:
            win.set_root_dir(d)

    return app.exec_()