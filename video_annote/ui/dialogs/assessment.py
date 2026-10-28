# video_annote/ui/dialogs/assessment.py
"""Confidence + notes prompt shown when a label is saved."""
from __future__ import annotations

from typing import Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QShortcut, QSlider, QTextEdit, QVBoxLayout, QWidget,
)

from ...core.models import SkillStep


def ask_confidence_and_notes(parent: Optional[QWidget], step: SkillStep, span_text: str) -> Optional[Tuple[int, str]]:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Step Self-Assessment")
    dlg.setModal(True)
    layout = QVBoxLayout(dlg)
    title = QLabel(f"Step {step.number}: {step.name}\n{span_text}")
    title.setTextInteractionFlags(Qt.TextSelectableByMouse)
    layout.addWidget(title)

    row = QHBoxLayout()
    row.addWidget(QLabel("Confidence (1-10):"))
    slider = QSlider(Qt.Horizontal)
    slider.setRange(1, 10)
    slider.setValue(5)
    slider.setTickInterval(1)
    slider.setTickPosition(QSlider.TicksBelow)
    value = QLabel("5")
    value.setMinimumWidth(24)
    value.setAlignment(Qt.AlignCenter)
    slider.valueChanged.connect(lambda v: value.setText(str(v)))
    row.addWidget(slider, stretch=1)
    row.addWidget(value)
    layout.addLayout(row)

    layout.addWidget(QLabel("Notes (optional):"))
    notes = QTextEdit()
    notes.setPlaceholderText("Add any additional context or explanation here...")
    notes.setFixedHeight(120)
    notes.setTabChangesFocus(True)  # Tab moves on to the buttons instead of typing a tab
    layout.addWidget(notes)

    save = QKeySequence("Ctrl+Return").toString(QKeySequence.NativeText)
    hint = QLabel(f"Type 1–9 or 0 (= 10) to set the confidence · {save} saves")
    hint.setStyleSheet("color: #888;")
    layout.addWidget(hint)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("Save Label")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    for key in ("Ctrl+Return", "Ctrl+Enter"):
        QShortcut(QKeySequence(key), dlg, activated=dlg.accept)
    for digit in "1234567890":  # (typed into the notes while they have the focus)
        QShortcut(QKeySequence(digit), dlg, activated=lambda d=digit: slider.setValue(int(d) or 10))
    slider.setFocus()
    if dlg.exec_() == QDialog.Accepted:
        return int(slider.value()), notes.toPlainText().strip()
    return None
