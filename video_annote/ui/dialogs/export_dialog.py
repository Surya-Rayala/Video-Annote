# video_annote/ui/dialogs/export_dialog.py
"""Options for exporting labeled clips."""
from __future__ import annotations

import os
from typing import Optional

from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ...media.export import ExportOptions
from ..common import app_settings, hand_cursor, hand_cursor_on_buttons

CAMERAS = [
    ("annotated", "The views shown when each label was recorded"),
    ("all", "Every video of the session"),
    ("time_source", "Only each label's time source"),
]
CHANNELS = [
    ("auto", "Automatic (mono when the recording is mono)"),
    ("mono", "Always mono"),
    ("keep", "As recorded"),
]
RATES = [(None, "As recorded"), (16000, "16 kHz"), (44100, "44.1 kHz"), (48000, "48 kHz")]
WAV_FORMATS = [("s16", "16-bit (opens everywhere)"), ("s24", "24-bit"),
               ("f32", "32-bit float (keeps every peak)")]


class ExportDialog(QDialog):
    def __init__(self, default_dir: str, n_annotations: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Export Labeled Clips")
        self.setModal(True)
        self.resize(620, 300)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Export {n_annotations} annotation(s) as clips holding exactly the frames you marked, with their sound. "
            "They go to <output folder>/<session>/, replacing an earlier export there.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        row = QHBoxLayout()
        self.out_edit = QLineEdit(default_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.out_edit, stretch=1)
        row.addWidget(browse)
        form.addRow("Output folder:", row)

        self.cameras = QComboBox()
        for key, text in CAMERAS:
            self.cameras.addItem(text, key)
        form.addRow("Videos:", self.cameras)

        self.write_video = QCheckBox("Video clips with sound (MP4), one per view")
        self.write_video.setChecked(True)
        self.write_wav = QCheckBox("Sound only (WAV), from each label's audio source")
        self.write_wav.setChecked(True)
        form.addRow("Outputs:", self.write_video)
        form.addRow("", self.write_wav)

        self.channels = QComboBox()
        for key, text in CHANNELS:
            self.channels.addItem(text, key)
        form.addRow("WAV channels:", self.channels)
        self.rate = QComboBox()
        for key, text in RATES:
            self.rate.addItem(text, key)
        form.addRow("WAV sample rate:", self.rate)
        self.wav_format = QComboBox()
        for key, text in WAV_FORMATS:
            self.wav_format.addItem(text, key)
        form.addRow("WAV format:", self.wav_format)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        hand_cursor(browse, self.cameras, self.channels, self.rate, self.wav_format)
        hand_cursor_on_buttons(buttons)
        self._restore_choices()

    # the choices of the last export are offered again (the folder stays per data root)
    _COMBOS = ("cameras", "channels", "rate", "wav_format")

    def _restore_choices(self) -> None:
        settings = app_settings()
        for name in self._COMBOS:  # (by option, not by its wording)
            combo, saved = getattr(self, name), settings.value(f"export/{name}", None, str)
            index = next((i for i in range(combo.count()) if str(combo.itemData(i)) == saved), -1)
            if index >= 0:
                combo.setCurrentIndex(index)
        self.write_video.setChecked(settings.value("export/write_video", "true", str) == "true")
        self.write_wav.setChecked(settings.value("export/write_wav", "true", str) == "true")

    def _save_choices(self) -> None:
        settings = app_settings()
        for name in self._COMBOS:
            settings.setValue(f"export/{name}", str(getattr(self, name).currentData()))
        settings.setValue("export/write_video", "true" if self.write_video.isChecked() else "false")
        settings.setValue("export/write_wav", "true" if self.write_wav.isChecked() else "false")

    def options(self) -> ExportOptions:
        return ExportOptions(
            out_dir=self.out_edit.text().strip(),
            cameras=self.cameras.currentData(),
            write_video=self.write_video.isChecked(),
            write_wav=self.write_wav.isChecked(),
            audio_channels=self.channels.currentData(),
            audio_sample_rate=self.rate.currentData(),
            wav_format=self.wav_format.currentData(),
        )

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export folder", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _on_accept(self) -> None:
        if not self.out_edit.text().strip():
            QMessageBox.warning(self, "No folder", "Choose an output folder.")
            return
        if not (self.write_video.isChecked() or self.write_wav.isChecked()):
            QMessageBox.warning(self, "Nothing to export", "Select MP4 clips and/or WAV audio.")
            return
        os.makedirs(self.out_edit.text().strip(), exist_ok=True)
        self._save_choices()
        self.accept()
