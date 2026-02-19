# video_annote/widgets/skills_panel.py
from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal, QEvent
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QPushButton, QSpinBox, QMessageBox, QGroupBox
)

from ..domain import RootConfig, SkillStep, get_skill_color_hex


class SkillsPanel(QGroupBox):
    """
    Right panel: persistent Skills/Steps list with unique colors and add/delete controls.

    Emits:
      - skill_selected(SkillStep | None)
      - skills_changed() whenever the skills list is modified
    """
    skill_selected = pyqtSignal(object)
    skills_changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Labels", parent)

        self._cfg: Optional[RootConfig] = None
        self._skills: List[SkillStep] = []

        self._build_ui()
        self._apply_clickable_cursors()

    # ---------------- UI ----------------

    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self.setLayout(layout)

        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list, stretch=1)

        add_row = QHBoxLayout()
        add_row.setSpacing(4)

        self.num_input = QSpinBox()
        self.num_input.setRange(1, 9999)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Label name")

        self.btn_add = QPushButton("Add Label")
        self.btn_add.clicked.connect(self._on_add_step)

        add_row.addWidget(QLabel("No:"))
        add_row.addWidget(self.num_input)
        add_row.addWidget(QLabel("Name:"))
        add_row.addWidget(self.name_input, stretch=1)
        add_row.addWidget(self.btn_add)

        layout.addLayout(add_row)

        self.btn_delete = QPushButton("Delete Selected Label")
        self.btn_delete.clicked.connect(self._on_delete_selected)
        layout.addWidget(self.btn_delete)

    # ---------------- Public API ----------------

    def set_config(self, cfg: Optional[RootConfig]) -> None:
        """
        Provide RootConfig to enable stable color mapping.
        """
        self._cfg = cfg
        self.refresh()

    def set_skills(self, skills: List[SkillStep]) -> None:
        self._skills = list(skills or [])
        self.refresh()

    def skills(self) -> List[SkillStep]:
        return list(self._skills)

    def selected_skill(self) -> Optional[SkillStep]:
        item = self.list.currentItem()
        if not item:
            return None
        return item.data(Qt.UserRole)

    def refresh(self) -> None:
        self.list.blockSignals(True)
        try:
            self.list.clear()
            for step in sorted(self._skills, key=lambda s: s.number):
                color_hex = get_skill_color_hex(step.number, self._cfg)
                item = QListWidgetItem(f"{step.number}: {step.name}")
                item.setData(Qt.UserRole, step)

                # show a small color square using background on a left margin by embedding unicode block
                # plus colorized background to be clearer.
                # We set foreground based on the color square in the decoration role
                item.setData(Qt.DecorationRole, self._color_swatch(color_hex))
                self.list.addItem(item)
        finally:
            self.list.blockSignals(False)

    # ---------------- Internals ----------------

    def _color_swatch(self, color_hex: str):
        # Qt accepts QColor directly for DecorationRole in many styles; fallback to brush-like if needed.
        c = QColor(color_hex)
        # Create a tiny pixmap-like swatch via QBrush is not directly supported here;
        # QColor in DecorationRole usually renders a small square.
        return c

    def _on_selection_changed(self, current: QListWidgetItem, previous: QListWidgetItem):
        step = current.data(Qt.UserRole) if current else None
        self.skill_selected.emit(step)

    def _on_add_step(self):
        num = int(self.num_input.value())
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Invalid", "Step name cannot be empty.")
            return

        existing = next((s for s in self._skills if s.number == num), None)
        if existing is not None:
            resp = QMessageBox.question(
                self,
                "Replace Step?",
                f"Step number {num} already exists:\n\n"
                f"Existing: {existing.number}: {existing.name}\n"
                f"New:      {num}: {name}\n\n"
                f"Replace the existing step?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
            # Replace by filtering out the existing
            self._skills = [s for s in self._skills if s.number != num]

        self._skills.append(SkillStep(number=num, name=name))
        self.name_input.clear()

        # Ensure a stable color assignment exists (if config provided)
        if self._cfg is not None:
            _ = get_skill_color_hex(num, self._cfg)

        self.refresh()
        self.skills_changed.emit()

    def _on_delete_selected(self):
        step = self.selected_skill()
        if not step:
            return
        resp = QMessageBox.question(
            self,
            "Delete Step?",
            f"Delete step {step.number}: {step.name}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return

        self._skills = [s for s in self._skills if not (s.number == step.number and s.name == step.name)]
        self.refresh()
        self.skills_changed.emit()
        self.skill_selected.emit(self.selected_skill())
    def _apply_clickable_cursors(self) -> None:
        # Buttons
        try:
            self.btn_add.setCursor(Qt.PointingHandCursor)
            self.btn_delete.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass

        # Inputs
        try:
            self.name_input.setCursor(Qt.IBeamCursor)
        except Exception:
            pass

        # List rows are clickable/selectable; use a hand cursor over the viewport.
        try:
            self.list.setMouseTracking(True)
            self.list.viewport().setMouseTracking(True)
            self.list.viewport().installEventFilter(self)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # Cursor feedback for list viewport
        try:
            if hasattr(self, "list") and obj is self.list.viewport():
                et = event.type()
                if et == QEvent.MouseMove:
                    it = self.list.itemAt(event.pos())
                    self.list.viewport().setCursor(Qt.PointingHandCursor if it is not None else Qt.ArrowCursor)
                elif et in (QEvent.Leave, QEvent.HoverLeave):
                    self.list.viewport().setCursor(Qt.ArrowCursor)
        except Exception:
            pass
        return super().eventFilter(obj, event)