# video_annote/ui/common.py
"""Small UI helpers shared by several widgets."""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QEvent, QObject, QSettings, QSize, Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QDialogButtonBox, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QSizePolicy,
    QTextEdit, QWidget,
)


def app_settings() -> QSettings:
    """Per-user preferences (window layout, last data root and session), in an INI file in the user's
    configuration folder. Tests point it at a scratch folder (tests/conftest.py)."""
    return QSettings(QSettings.IniFormat, QSettings.UserScope, "video-annote", "video-annote")


class ElidedLabel(QLabel):
    """A one-line label that shortens its text in the middle to fit (full text in the tooltip), so a
    long path never makes the window wider than the screen."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(80)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt API)
        self._full = str(text or "")
        self.setToolTip(self._full)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, max(10, self.width() - 4)))


def hand_cursor(*widgets: Optional[QWidget]) -> None:
    """Pointing-hand cursor for clickable widgets."""
    for w in widgets:
        if w is not None:
            w.setCursor(Qt.PointingHandCursor)


def hand_cursor_on_buttons(box: QDialogButtonBox) -> None:
    hand_cursor(*box.buttons())


class TextFieldsKeepKeys(QObject):
    """Return/Enter and Esc reach a text field or spin box that has the focus (to finish or abandon what
    is being typed: adding a label, editing a table cell) instead of the window's shortcuts for them.
    Installed on the application; other keys a text field uses it claims by itself."""
    KEYS = (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if (event.type() == QEvent.ShortcutOverride and event.key() in self.KEYS
                and isinstance(obj, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit))):
            event.accept()  # the key goes to the field as a normal key press
            return True
        return False


class _WheelOnlyWhenFocused(QObject):
    """Wheel events reach a combo box only when it has the focus: scrolling over the window must not
    switch its value (and a switched time source is saved at once)."""

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if event.type() == QEvent.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return False


def calm_combos(*combos: QWidget) -> None:
    """Combo boxes that a click does not focus (so ←/→/Space keep stepping and playing the video) and
    that a wheel scrolled over the window does not change."""
    for combo in combos:
        combo.setFocusPolicy(Qt.TabFocus)
        guard = _WheelOnlyWhenFocused(combo)
        combo.installEventFilter(guard)


class _ItemHoverFilter(QObject):
    """Shows a pointing hand while the mouse is over an item of a list/table view."""

    def __init__(self, view: QAbstractItemView):
        super().__init__(view)
        self._view = view

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if event.type() in (QEvent.MouseMove, QEvent.HoverMove):
            over_item = self._view.indexAt(event.pos()).isValid()
            self._view.viewport().setCursor(Qt.PointingHandCursor if over_item else Qt.ArrowCursor)
        elif event.type() in (QEvent.Leave, QEvent.HoverLeave):
            self._view.viewport().setCursor(Qt.ArrowCursor)
        return False


def hand_cursor_on_items(view: QAbstractItemView) -> None:
    view.setMouseTracking(True)
    view.viewport().setMouseTracking(True)
    view.viewport().installEventFilter(_ItemHoverFilter(view))


def show_error(parent: Optional[QWidget], title: str, message: str, details: str = "") -> None:
    """Error box with the technical part (ffmpeg log, traceback) folded under "Show Details"."""
    box = QMessageBox(QMessageBox.Critical, title, message, QMessageBox.Ok, parent)
    if details:
        box.setDetailedText(details)
    box.exec_()


def split_error(text: str):
    """First paragraph of an error for the dialog, the rest for "Show Details"."""
    head, _, tail = str(text).partition("\n\n")
    return head.strip(), tail.strip()
