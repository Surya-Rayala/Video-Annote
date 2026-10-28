# video_annote/ui/widgets/start_time.py
"""Typing when a recording started, as Unix time or as a date and time written the way the recording
software wrote it (year, month or day first, in a time zone), with the moment it stands for shown as
you type."""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import List, Optional
from zoneinfo import available_timezones

from PyQt5.QtCore import QEvent, Qt, QTimeZone, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from ...media.recorded import (
    START_FORMS, ZONE_NAMES, EnteredTime, ReadTime, forms_that_read, read_entered, utc_offset_text,
)
from ..common import app_settings, hand_cursor, hand_cursor_on_buttons

OTHER_ZONE = "other"


def readable_time(when: datetime, precision_s: float, zone: Optional[tzinfo] = None) -> str:
    """Fri 20 Mar 2026, 11:37:15.203 (UTC−05:00) in this computer's time zone (or ``zone``)."""
    shown = when.astimezone(zone)
    fraction = f".{shown.microsecond // 1000:03d}" if precision_s < 1 else ""
    return f"{shown:%a %d %b %Y, %H:%M:%S}{fraction} ({utc_offset_text(when, zone)})"


@lru_cache(maxsize=1)
def all_zones() -> List[str]:
    """The IANA time zone names this computer knows, UTC first."""
    names = {n for n in available_timezones()
             if "/" in n and not n.startswith(("Etc/", "SystemV/", "posix/", "right/"))}
    return ["UTC"] + sorted(names)


def this_computers_zone() -> str:
    """This computer's IANA time zone name ("" if it has none)."""
    name = bytes(QTimeZone.systemTimeZoneId()).decode(errors="replace")
    return name if name in all_zones() else ""


class StartTimeEdit(QWidget):
    """How a recording start is written (and a date and time's time zone), the field to type it in and,
    under it, the moment it stands for (or why it cannot be read). The format and time zone chosen
    last are offered again."""
    confirmed = pyqtSignal()  # Return pressed with a usable value
    changed = pyqtSignal()  # the text, format or time zone changed

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        choices = QHBoxLayout()
        choices.setSpacing(6)
        choices.addWidget(QLabel("Format"))
        self.form_combo = QComboBox()
        for key, (name, example, _examples) in START_FORMS.items():
            self.form_combo.addItem(f"{name} ({example})", key)
        choices.addWidget(self.form_combo)
        choices.addSpacing(12)
        self.zone_title = QLabel("Time zone")
        choices.addWidget(self.zone_title)
        self.zone_combo = QComboBox()
        self.zone_combo.setToolTip("The time zone of the clock that wrote the time (a time zone written in the "
                                   "time itself is used instead)")
        here = this_computers_zone()
        self.zone_combo.addItem(f"{ZONE_NAMES.get(here, here)} (this computer's)" if here
                                else "This computer's time zone", here)
        for zone, name in ZONE_NAMES.items():
            if zone != here:
                self.zone_combo.addItem(name, zone)
        self.zone_combo.addItem("Other…", OTHER_ZONE)
        choices.addWidget(self.zone_combo)
        choices.addStretch()
        layout.addLayout(choices)
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        self.edit.installEventFilter(self)  # Return confirms the time, and never presses a dialog's default button
        self.row = QHBoxLayout()
        self.row.setSpacing(6)
        self.row.addWidget(self.edit, stretch=1)
        layout.addLayout(self.row)
        self.preview = QLabel("")
        self.preview.setTextFormat(Qt.RichText)
        self.preview.setWordWrap(True)
        self.preview.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.preview.setMinimumHeight(2 * self.preview.fontMetrics().lineSpacing())  # (no jumps as it wraps)
        layout.addWidget(self.preview)

        self._zone_index = 0
        self.reset()
        self.edit.textChanged.connect(self._show)
        self.form_combo.currentIndexChanged.connect(self._show)
        self.form_combo.activated.connect(lambda _i: self._remember())
        self.zone_combo.currentIndexChanged.connect(self._show)
        self.zone_combo.activated.connect(self._on_zone_chosen)
        self._show()

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if obj is self.edit and event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.value() is not None:
                self.confirmed.emit()
            return True
        return False

    # ---------------- what is typed ----------------

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self.edit.setText(text)

    def form(self) -> str:
        return str(self.form_combo.currentData())

    def zone(self) -> str:
        if self.form() == "unix":
            return ""
        index = self.zone_combo.currentIndex()
        if self.zone_combo.itemData(index) == OTHER_ZONE:  # (while one is being chosen)
            index = self._zone_index
        return str(self.zone_combo.itemData(index) or "")

    def entered(self) -> EnteredTime:
        return EnteredTime(self.text(), self.form(), self.zone())

    def set_entered(self, entered: EnteredTime) -> None:
        """Show a time typed before, in the format (and time zone) it was typed in."""
        self.form_combo.setCurrentIndex(max(self.form_combo.findData(entered.form), 0))
        if entered.form != "unix":
            self._select_zone(entered.zone)
        self.edit.setText(entered.text)

    def reset(self) -> None:
        """Empty, in the format and time zone chosen last."""
        settings = app_settings()
        self.form_combo.setCurrentIndex(max(self.form_combo.findData(str(settings.value("start/form", "unix"))), 0))
        self._select_zone(str(settings.value("start/zone", "") or ""))
        self.edit.clear()

    def value(self) -> Optional[ReadTime]:
        """The time typed, or None if the field is empty or cannot be read."""
        try:
            return read_entered(self.entered()) if self.text() else None
        except ValueError:
            return None

    # ---------------- format and time zone ----------------

    def _select_zone(self, zone: str) -> None:
        index = self.zone_combo.findData(zone) if zone else 0
        if index < 0 and zone in all_zones():
            index = self.zone_combo.count() - 1  # before Other…
            self.zone_combo.insertItem(index, ZONE_NAMES.get(zone, zone), zone)
        self.zone_combo.setCurrentIndex(max(index, 0))
        self._zone_index = self.zone_combo.currentIndex()

    def _on_zone_chosen(self, index: int) -> None:
        if self.zone_combo.itemData(index) == OTHER_ZONE:
            zones = all_zones()
            current = str(self.zone_combo.itemData(self._zone_index) or this_computers_zone())
            name, ok = QInputDialog.getItem(self, "Time Zone", "Time zone of the clock that wrote the time "
                                            "(type to search):", zones, zones.index(current) if current in zones else 0,
                                            True)
            known = {z.lower(): z for z in zones}.get(name.strip().lower()) if ok else None
            if known is None:
                if ok:
                    QMessageBox.warning(self, "Time Zone", f"“{name.strip()}” is not a time zone this computer knows.")
                self.zone_combo.setCurrentIndex(self._zone_index)
                return
            self._select_zone(known)
        self._zone_index = self.zone_combo.currentIndex()
        self._remember()

    def _remember(self) -> None:
        settings = app_settings()
        settings.setValue("start/form", self.form())
        settings.setValue("start/zone", str(self.zone_combo.itemData(self._zone_index) or ""))

    # ---------------- what it stands for ----------------

    def _other_forms(self) -> str:
        """The formats that read the text, if the chosen one does not (or reads it as a future time)."""
        others = [f for f in forms_that_read(self.text()) if f != self.form()]
        if "year-first" in others:  # (a year-first date reads the same in every date format)
            others = ["year-first"]
        return " or ".join(f"“{START_FORMS[f][0]}”" for f in others)

    def _show(self) -> None:
        unix = self.form() == "unix"
        self.zone_title.setVisible(not unix)
        self.zone_combo.setVisible(not unix)
        self.edit.setPlaceholderText(f"e.g. {START_FORMS[self.form()][2]}")
        t: Optional[ReadTime] = None
        problem = ""
        if self.text():
            try:
                t = read_entered(self.entered())
            except ValueError as e:
                others = self._other_forms()
                problem = f"Choose {others} under Format to read this." if others else str(e)
        self.zone_combo.setEnabled(t is None or not t.zone_typed)
        self.preview.setText(self._describe(t) if t is not None else
                             f"<span style='color:#c0392b'>{html.escape(problem)}</span>" if problem else "")
        self.changed.emit()

    def _describe(self, t: ReadTime) -> str:
        shown = t.when.astimezone(t.zone)

        def clock(moment: datetime) -> str:
            text = f"{moment:%H:%M:%S}" + (f".{moment.microsecond // 1000:03d}" if t.precision_s < 1 else "")
            return text if moment.date() == shown.date() else f"{moment:%a %d %b}, {text}"

        first = f"<b>{html.escape(readable_time(t.when, t.precision_s, t.zone))}</b>"
        here = t.when.astimezone()
        parts = [first]
        if shown.utcoffset() != here.utcoffset():  # typed in another time zone: what it is here
            if not t.zone_typed:
                parts[0] += " " + html.escape(ZONE_NAMES.get(self.zone(), self.zone()))
            parts.append(f"{clock(here)} here")
        parts.append(f"{clock(t.when.astimezone(timezone.utc))} UTC")
        if t.unit not in ("", "seconds"):
            parts.append(f"read as {t.unit}")
        if t.when > datetime.now(timezone.utc) + timedelta(days=1):
            others = self._other_forms()
            warning = "in the future: check it" + (f" (or choose {others} under Format)" if others else "")
            parts.append(f"<span style='color:#d35400'>{html.escape(warning)}</span>")
        return " &nbsp;·&nbsp; ".join(parts)


def ask_recording_start(parent: Optional[QWidget], video: str, current: str = "", entered: bool = False
                        ) -> Optional[EnteredTime]:
    """Ask when ``video`` started recording (``current``: the Unix time it has now). Returns the time
    typed, an empty one to go back to the time the file states (``entered``: a time was entered
    before), or None if cancelled."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Recording Start")
    dlg.setMinimumWidth(760)
    layout = QVBoxLayout(dlg)
    intro = QLabel(f"When did <b>{html.escape(video)}</b> start recording? Choose how the time is written, "
                   "then type it.")
    intro.setWordWrap(True)
    layout.addWidget(intro)
    field = StartTimeEdit()
    if current:
        field.set_entered(EnteredTime(current))
    layout.addWidget(field)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    ok = buttons.button(QDialogButtonBox.Ok)
    ok.setText("Confirm")
    result = {"value": None}
    if entered:
        revert = QPushButton("Use the File's Time")
        revert.setToolTip("Forget the time entered by hand")
        buttons.addButton(revert, QDialogButtonBox.ResetRole)
        revert.clicked.connect(lambda: (result.update(value=EnteredTime("")), dlg.accept()))
        hand_cursor(revert)
    field.changed.connect(lambda: ok.setEnabled(field.value() is not None))
    ok.setEnabled(field.value() is not None)
    buttons.accepted.connect(lambda: (result.update(value=field.entered()), dlg.accept()))
    buttons.rejected.connect(dlg.reject)
    field.confirmed.connect(lambda: (result.update(value=field.entered()), dlg.accept()))
    layout.addWidget(buttons)
    hand_cursor_on_buttons(buttons)
    field.edit.setFocus()
    if dlg.exec_() != QDialog.Accepted:
        return None
    return result["value"]
