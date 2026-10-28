# video_annote/ui/widgets/skills_panel.py
"""Right-hand panel: the persistent list of labels (number + name) with their stable colors."""
from __future__ import annotations

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QGroupBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ...core.colors import color_for_step
from ...core.models import SkillStep, clean_text
from ..common import hand_cursor, hand_cursor_on_items


class SkillsPanel(QGroupBox):
    skill_selected = pyqtSignal(object)  # SkillStep | None
    skills_changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Labels", parent)
        self._color_map: Optional[Dict[int, int]] = None
        self._skills: List[SkillStep] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self.list = QListWidget()
        self.list.setWordWrap(True)  # long names wrap instead of being cut off (or scrolled sideways)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # a fixed text width: a scroll bar appearing after the layout would otherwise make wrapped rows overlap
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setToolTip("Select the label to record next, then Start (or press I)")
        self.list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list, stretch=1)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.num_input = QSpinBox()
        self.num_input.setRange(1, 9999)
        self.num_input.setPrefix("No. ")
        self.num_input.setToolTip("Number of the new label (a label with this number is replaced)")
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("New label name")
        self.name_input.setMinimumWidth(100)
        self.name_input.returnPressed.connect(self._on_add)
        row.addWidget(self.num_input)
        row.addWidget(self.name_input, stretch=1)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.btn_add = QPushButton("Add Label")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_delete = QPushButton("Delete Label")
        self.btn_delete.setToolTip("Remove the selected label from the list (annotations that use it are kept)")
        self.btn_delete.clicked.connect(self._on_delete)
        buttons.addWidget(self.btn_add)
        buttons.addWidget(self.btn_delete)
        layout.addLayout(buttons)
        self.setMinimumWidth(220)
        hand_cursor(self.btn_add, self.btn_delete)
        hand_cursor_on_items(self.list)

    # ---------------- public API ----------------

    def set_color_map(self, color_map: Optional[Dict[int, int]]) -> None:
        """The config's label -> palette index map (updated in place as labels are added)."""
        self._color_map = color_map
        self.refresh()

    def set_skills(self, skills: List[SkillStep]) -> None:
        self._skills = list(skills or [])
        self.refresh()
        self._suggest_number()

    def _suggest_number(self) -> None:
        """Offer the number after the highest one in use for the next label."""
        self.num_input.setValue(min(9999, max((s.number for s in self._skills), default=0) + 1))

    def skills(self) -> List[SkillStep]:
        return list(self._skills)

    def selected_skill(self) -> Optional[SkillStep]:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def refresh(self) -> None:
        current = self.selected_skill()
        self.list.blockSignals(True)
        try:
            self.list.clear()
            for step in sorted(self._skills, key=lambda s: s.number):
                item = QListWidgetItem(f"{step.number}: {step.name}")
                item.setToolTip(f"{step.number}: {step.name}")
                item.setData(Qt.UserRole, step)
                item.setData(Qt.DecorationRole, QColor(color_for_step(step.number, self._color_map)))
                self.list.addItem(item)
                if current is not None and step.number == current.number:
                    self.list.setCurrentItem(item)
        finally:
            self.list.blockSignals(False)
        selected = self.selected_skill()
        if selected != current:
            self.skill_selected.emit(selected)

    # ---------------- actions ----------------

    def _on_selection_changed(self, current: Optional[QListWidgetItem], _previous) -> None:
        self.skill_selected.emit(current.data(Qt.UserRole) if current else None)

    def _on_add(self) -> None:
        number = int(self.num_input.value())
        name = clean_text(self.name_input.text())
        if not name:
            QMessageBox.warning(self, "Invalid", "Label name cannot be empty.")
            return
        existing = next((s for s in self._skills if s.number == number), None)
        if existing is not None:
            if existing.name == name:
                return
            resp = QMessageBox.question(
                self, "Replace label?",
                f"Label {number} already exists:\n\n  Existing: {existing.name}\n  New:      {name}\n\n"
                "Annotations already saved keep the name they were recorded with; only new "
                "annotations will use the new name. Replace?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
            self._skills = [s for s in self._skills if s.number != number]
        self._skills.append(SkillStep(number=number, name=name))
        if self._color_map is not None:
            color_for_step(number, self._color_map)  # allocate a stable color now
        self.name_input.clear()
        self.refresh()
        self._suggest_number()
        self.skills_changed.emit()

    def _on_delete(self) -> None:
        step = self.selected_skill()
        if step is None:
            return
        resp = QMessageBox.question(
            self, "Delete label?",
            f"Delete label {step.number}: {step.name}?\n\nAnnotations that use it are kept.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        self._skills = [s for s in self._skills if s != step]
        self.refresh()
        self.skills_changed.emit()
