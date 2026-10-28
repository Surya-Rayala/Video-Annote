# video_annote/media/recorded.py
"""When a recording was made, in real-world time: as the file states it, or as entered by hand.

Recording software states this in different places and forms; every one ffprobe exposes is read:

* The file's creation time: macOS/QuickTime screen recordings, MP4 writers, Matroska, MXF and ASF
  state it in UTC (whole seconds in MP4/MOV, up to microseconds in Matroska); AVI states the
  recording computer's local time without a time zone. (Some cameras write local time even where
  the format says UTC; nothing in the file tells.)
* ``com.apple.quicktime.creationdate`` (iPhone, iPad, Apple apps): local time with its UTC offset.
* Other date tags: ``creation_date``, ``date_recorded`` (Matroska), ``date_digitized``, ``date``
  (MP4 ©day), when they hold a time of day.
* HLS streams: the EXT-X-PROGRAM-DATE-TIME of the first segment (usually milliseconds).
* Tobii Pro Glasses 3: ``recording.g3`` next to ``scenevideo.mp4`` (microseconds, UTC).

A time stated without a time zone is read as this computer's local time, and the block says so.
File names and file dates are never used: on the project's recordings the NVR exports' names are
an hour off their own file dates and 50 s off the recordings' true relative start (mains-hum
matching, see docs/audio-investigation.md), and copied files carry file dates hours off.

A video's "recorded" block in session.json::

    "recorded": {
      "start": "2026-03-20T11:37:15-05:00",   when the video's t = 0 was recorded (local time, UTC offset)
      "end": "2026-03-20T11:50:02-05:00",     when its last frame ended
      "start_unix": 1774024635,               the same moments in Unix time (seconds since 1970, UTC)
      "end_unix": 1774025401.8,
      "from": "the file's creation time, to the second"
    }

``start`` and ``end`` have as many decimals as the source gives (none for whole seconds);
``start_unix``/``end_unix`` keep every digit. A moment t seconds into the video happened at
start_unix + t. When the file states nothing: ``{"start": null, "from": "not stated in the file"}``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .probe import MediaInfo

EARLIEST_YEAR = 1990  # earlier dates come from clocks that were never set (1904, 1970, 1980 epochs)
TOBII_FILE = "recording.g3"
# 2026-03-20T18:21:23.203735Z, 2026-03-20 11:37:15-0500, 2026:03:20 11:37:15 (EXIF style), ...
_TIMESTAMP = re.compile(r"(\d{4})[-:](\d{2})[-:](\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?"
                        r"\s*(Z|UTC|GMT|[+-]\d{2}(?::?\d{2})?)?$", re.IGNORECASE)
# tags that can state when a recording was made, most specific first, and how to name them for people
_TIME_KEYS = {"com.apple.quicktime.creationdate": "creation date", "creation_time": "creation time",
              "creation_date": "creation date", "date_recorded": "recording date",
              "date_digitized": "date", "date": "date"}
NOT_STATED = "not stated in the file"


class Parsed(NamedTuple):
    when: datetime  # UTC
    precision_s: float  # resolution of the stated value
    offset: str  # the UTC offset the value states ("" for Z/UTC or when it states none)
    assumed: bool  # it states no time zone: read as this computer's local time


@dataclass(frozen=True)
class Stamp:
    """A real-world time for a recording's start (or, ``at_first_sample``, an HLS stream's first sample)."""
    when: datetime  # UTC
    precision_s: float
    source: str  # where it comes from, for people ("the file's creation time")
    assumed_zone: bool = False  # it stated no time zone: read as this computer's local time
    at_first_sample: bool = False


def _offset_text(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    return f"{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def parse_timestamp(text: Any, whole_if_zero: bool = False, local_if_no_zone: bool = False) -> Optional[Parsed]:
    """Read a date and time of day; None for anything else, for dates before EARLIEST_YEAR, and for a
    value without a time zone unless ``local_if_no_zone`` (then it is read as this computer's local
    time). ``whole_if_zero``: a fraction of zeros means whole seconds (ffprobe prints six decimals for
    an MP4 creation time, which only holds whole seconds)."""
    m = _TIMESTAMP.match(str(text or "").strip())
    if not m or (not m.group(8) and not local_if_no_zone):
        return None
    digits, zone = m.group(7) or "", (m.group(8) or "").upper()
    fields = [int(m.group(i)) for i in range(1, 7)] + [int((digits + "000000")[:6])]
    try:
        if not zone:
            when = datetime(*fields).astimezone()  # this computer's zone (with daylight saving on that date)
        elif zone in ("Z", "UTC", "GMT"):
            when = datetime(*fields, tzinfo=timezone.utc)
        else:
            hours, minutes = int(zone[1:3]), int(zone[-2:]) if len(zone) > 3 else 0
            when = datetime(*fields, tzinfo=timezone((-1 if zone[0] == "-" else 1)
                                                    * timedelta(hours=hours, minutes=minutes)))
    except (ValueError, OverflowError):  # e.g. month 13, or an offset of a day or more
        return None
    if when.year < EARLIEST_YEAR:
        return None
    precision = 1.0 if not digits or (whole_if_zero and not digits.strip("0")) else 10.0 ** -min(len(digits), 6)
    stated = _offset_text(when.utcoffset() or timedelta(0)) if zone not in ("Z", "UTC", "GMT") else ""
    return Parsed(when.astimezone(timezone.utc), precision, stated, not zone)


class UnixTime(NamedTuple):
    when: datetime  # UTC
    precision_s: float
    unit: str  # how the number was read: "seconds", "milliseconds", "microseconds" or "nanoseconds"


# a Unix time's unit, told apart by the number of digits before the decimal point (for 1973-2286)
_UNITS = [(11, "seconds", 0), (14, "milliseconds", 3), (17, "microseconds", 6), (20, "nanoseconds", 9)]


def parse_unix_time(text: Any) -> UnixTime:
    """A typed Unix timestamp (time since 1970-01-01 UTC): seconds such as 1774024635 or
    1774024635.203, or milliseconds, microseconds or nanoseconds (13, 16 or 19 digits). Raises
    ValueError with a short explanation."""
    s = str(text or "").strip().replace("_", "").replace(" ", "")
    m = re.fullmatch(r"(\d+)(?:\.(\d*))?", s)
    if not m:
        raise ValueError("Type a Unix timestamp: seconds since 1970, e.g. 1774024635 or 1774024635.203.")
    whole, fraction = m.group(1).lstrip("0") or "0", m.group(2) or ""
    unit = next((u for u in _UNITS if len(whole) <= u[0]), None)
    if unit is None:
        raise ValueError("That number is too long for a Unix timestamp.")
    try:  # exact, in whole microseconds (a float would be off by one for 16-digit values)
        micro = int((Decimal(s) * Decimal(10) ** (6 - unit[2])).to_integral_value(rounding=ROUND_HALF_EVEN))
    except InvalidOperation:
        raise ValueError("That is not a number.") from None
    when = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micro)
    if when.year < EARLIEST_YEAR:
        raise ValueError("That is before 1990: not a recording time (is a digit missing?).")
    precision = max(10.0 ** -(unit[2] + len(fraction)), 1e-6)  # (times are kept to the microsecond)
    return UnixTime(when, precision, unit[1])


# ---------------- a recording start typed by hand ----------------

# The forms a recording start can be typed in: Unix time, or a date and time as devices and recording
# software write it (in file names, logs, on screen). Dates that begin with the year or name the
# month read the same in every date form; the form decides how 10/11/2026 is read.
START_FORMS = {  # key: (name, short example, examples)
    "unix": ("Unix time", "1774024635", "1774024635 or 1774024635.203 (milliseconds work too)"),
    "year-first": ("Year first", "2026-10-26 15:27:32", "20261026152732 or 2026-10-26 15:27:32"),
    "month-first": ("Month first", "10/26/2026 3:27:32 PM", "10/26/2026 3:27:32 PM or Oct 26, 2026 15:27:32"),
    "day-first": ("Day first", "26/10/2026 15:27:32", "26/10/2026 15:27:32 or 26 Oct 2026 15:27:32"),
}
# time zones offered by name for a typed date and time (any other IANA name works too)
ZONE_NAMES = {
    "America/New_York": "US Eastern time", "America/Chicago": "US Central time",
    "America/Denver": "US Mountain time", "America/Phoenix": "Arizona time",
    "America/Los_Angeles": "US Pacific time", "America/Anchorage": "Alaska time",
    "Pacific/Honolulu": "Hawaii time", "UTC": "UTC",
}


class EnteredTime(NamedTuple):
    """A recording start as someone typed it (none when ``text`` is empty)."""
    text: str
    form: str = "unix"  # a START_FORMS key
    zone: str = ""  # a date and time's time zone unless it states one: an IANA name ("" = this computer's)


class ReadTime(NamedTuple):
    when: datetime  # UTC
    precision_s: float
    said: str  # what was typed and how it was read ("20261026152732, US Eastern time")
    unit: str = ""  # how a Unix time was read: "seconds", "milliseconds", "microseconds" or "nanoseconds"
    zone: Optional[tzinfo] = None  # the time zone a date and time was read in (None: this computer's)
    zone_typed: bool = False  # the date and time stated it


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_NAME = r"[a-z]{3,9}\.?"
_DATES = {  # ways of writing a date
    "ymd": r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})",  # 20261026
    "y-m-d": rf"(?P<y>\d{{4}})(?P<ds>[-/.:_ ])(?P<m>\d{{1,2}}|{_NAME})(?P=ds)(?P<d>\d{{1,2}})",  # 2026-10-26
    "m/d/y": r"(?P<m>\d{1,2})(?P<ds>[-/.])(?P<d>\d{1,2})(?P=ds)(?P<y>\d{4})",  # 10/26/2026
    "d/m/y": r"(?P<d>\d{1,2})(?P<ds>[-/.])(?P<m>\d{1,2})(?P=ds)(?P<y>\d{4})",  # 26/10/2026, 26.10.2026
    "mon-d-y": rf"(?P<m>{_NAME})[ -]?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?[ -](?P<y>\d{{4}})",  # Oct 26, 2026
    "d-mon-y": rf"(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[ -]?(?P<m>{_NAME}),?[ -](?P<y>\d{{4}})",  # 26 Oct 2026
}
_FORM_DATES = {"year-first": ("ymd", "y-m-d", "mon-d-y", "d-mon-y"),
               "month-first": ("ymd", "y-m-d", "m/d/y", "mon-d-y", "d-mon-y"),
               "day-first": ("ymd", "y-m-d", "d/m/y", "mon-d-y", "d-mon-y")}
# then the time of day (15:27:32, 152732, 15-27-32, 3.27.32 PM), a fraction of a second (digits right
# after a compact time: Hikvision's 20261026152732123) and the time zone if it is stated
_TIME = (r"(?:,? at |[ ,_t-]+|)(?P<H>\d{1,2})(?P<ts>[:._-]?)(?P<M>\d{2})(?P=ts)(?P<S>\d{2})"
         r"(?:[.,](?P<F>\d{1,9})|(?P<F2>\d{1,9}))?(?: ?(?P<AP>[ap])\.?m\.?)?"
         r"(?: ?(?P<Z>z|utc|gmt|(?:utc|gmt)? ?[+-]\d{1,2}(?::?\d{2})?))?")
_DATE_TIMES = {kind: re.compile(date + _TIME, re.IGNORECASE) for kind, date in _DATES.items()}


def _month(text: str) -> int:
    """1-12 for a month's number or (English) name, at least three letters of it; 0 for anything else."""
    t = text.lower().rstrip(".")
    if t.isdigit():
        return int(t)
    return next((i for i, name in enumerate(_MONTHS, 1) if len(t) >= 3 and name.startswith(t)), 0)


def _zone(name: str) -> tzinfo:
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone {name!r}.") from None


def _stated_zone(text: str) -> tzinfo:
    """The time zone a date and time states: Z, UTC, GMT, +05:30, -0400, GMT-4."""
    z = text.upper().replace(" ", "").replace(":", "").lstrip("UTCGM")
    if z in ("", "Z"):
        return timezone.utc
    digits = z[1:]
    hours, minutes = (int(digits[:-2]), int(digits[-2:])) if len(digits) > 2 else (int(digits), 0)
    if hours > 14 or minutes > 59:
        raise ValueError("That time zone does not exist.")
    return timezone((-1 if z[0] == "-" else 1) * timedelta(hours=hours, minutes=minutes))


def _read_date_time(text: str, form: str, zone: str) -> ReadTime:
    typed = re.sub(r"\s+", " ", str(text or "")).strip()  # (macOS writes a narrow space before PM)
    s = re.sub(r" ?\([^()]*\)$", "", typed.replace("−", "-"))  # (−: as the app prints offsets)
    s = re.sub(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,? ", "", s, flags=re.IGNORECASE)  # Monday, ...
    kind, m = "", None
    for kind in _FORM_DATES[form]:
        m = _DATE_TIMES[kind].fullmatch(s)
        if m:
            break
    if m is None:
        abbreviation = re.fullmatch(r"(.*) ([A-Z]{3,5})", s)  # EDT, CEST: the same letters name different zones
        if abbreviation and any(_DATE_TIMES[k].fullmatch(abbreviation.group(1)) for k in _FORM_DATES[form]):
            raise ValueError(f"Leave out {abbreviation.group(2)} and choose the time zone instead.")
        raise ValueError(f"Type the date and time like {START_FORMS[form][2]}.")
    hour, fraction = int(m.group("H")), m.group("F") or m.group("F2") or ""
    try:
        if m.group("F2") and m.group("ts"):  # 15:27:32123
            raise ValueError
        if m.group("AP"):
            if not 1 <= hour <= 12:
                raise ValueError
            hour = hour % 12 + (12 if m.group("AP").lower() == "p" else 0)
        naive = datetime(int(m.group("y")), _month(m.group("m")), int(m.group("d")), hour, int(m.group("M")),
                         int(m.group("S")), int((fraction + "000000")[:6]))
    except ValueError:
        raise ValueError("There is no such date or time: check the numbers.") from None
    said = typed + {"m/d/y": " as month/day/year", "d/m/y": " as day/month/year"}.get(kind, "")
    if m.group("Z"):
        tz: Optional[tzinfo] = _stated_zone(m.group("Z"))
        when = naive.replace(tzinfo=tz)
    elif zone:
        tz = _zone(zone)
        when = naive.replace(tzinfo=tz)
        said += f", {ZONE_NAMES.get(zone, zone + ' time')}"
    else:  # this computer's time zone, with daylight saving as on that date
        tz, when = None, naive.astimezone()
        said += ", this computer's time zone"
    if when.year < EARLIEST_YEAR:
        raise ValueError("That is before 1990: not a recording time.")
    precision = max(10.0 ** -len(fraction), 1e-6) if fraction else 1.0
    return ReadTime(when.astimezone(timezone.utc), precision, said, zone=tz, zone_typed=bool(m.group("Z")))


def read_entered(entered: EnteredTime) -> ReadTime:
    """When a typed recording start was (ValueError with a short explanation if it cannot be read)."""
    if entered.form == "unix":
        t = parse_unix_time(entered.text)
        return ReadTime(t.when, t.precision_s, f"Unix time {str(entered.text).strip()}", t.unit)
    if entered.form not in _FORM_DATES:
        raise ValueError(f"Unknown time format {entered.form!r}.")
    return _read_date_time(entered.text, entered.form, entered.zone)


def forms_that_read(text: str) -> List[str]:
    """The START_FORMS that can read ``text`` (Unix time only for a time before tomorrow), to point at
    the right one."""
    found = []
    for form in START_FORMS:
        try:
            t = read_entered(EnteredTime(text, form, "UTC"))
        except ValueError:
            continue
        if form != "unix" or t.when < datetime.now(timezone.utc) + timedelta(days=1):
            found.append(form)
    return found


def entered_stamp(entered: EnteredTime) -> Stamp:
    """The recording start someone typed (see read_entered)."""
    t = read_entered(entered)
    return Stamp(t.when, t.precision_s, f"entered by hand ({t.said})")


def _precision_words(precision_s: float) -> str:
    return ("to the second" if precision_s >= 1 else "to the millisecond" if precision_s >= 0.001
            else "to the microsecond")


def _tobii(source: str, info: MediaInfo) -> Optional[Stamp]:
    """The start a Tobii Pro Glasses 3 recording folder states for ``source``: only when its
    recording.g3 names this file and states this video's length (an edited or re-encoded copy keeps
    neither)."""
    path = os.path.join(os.path.dirname(os.path.abspath(source)), TOBII_FILE)
    try:
        with open(path, "rb") as f:
            meta = json.loads(f.read(1_000_000))
    except (OSError, ValueError):
        return None
    camera = meta.get("scenecamera") if isinstance(meta, dict) else None
    if not isinstance(camera, dict) or camera.get("file") != os.path.basename(source):
        return None
    created, duration = meta.get("created"), meta.get("duration")
    v = info.video
    # recording.g3's duration is the end of the scene video's last frame (measured on 10 recordings)
    if (v is None or not v.duration or not isinstance(duration, (int, float))
            or abs(float(duration) - (v.start_time + v.duration)) > 0.5):
        return None
    parsed = parse_timestamp(created)
    if parsed is None:
        return None
    return Stamp(parsed.when, parsed.precision_s, "the Tobii recording file (recording.g3)")


def _stream_tags(info: MediaInfo, index: Optional[int]) -> Dict[str, Any]:
    for s in info.raw.get("streams") or []:
        if index is not None and s.get("index") == index:
            return s.get("tags") or {}
    return {}


def find_recording_time(source: str, info: MediaInfo, program_date_time: Optional[str] = None) -> Optional[Stamp]:
    """The most precise recording time ``source`` states, or None. ``program_date_time``: an HLS
    stream's first EXT-X-PROGRAM-DATE-TIME (see ingest._hls_playlist)."""
    found: List[Tuple[int, Stamp]] = []  # (preference: lower is better, stamp)
    if "://" not in source:
        tobii = _tobii(source, info)
        if tobii is not None:
            found.append((0, tobii))
    if program_date_time:
        parsed = parse_timestamp(program_date_time, local_if_no_zone=True)  # (the HLS spec wants a zone)
        if parsed is not None:
            found.append((1 + (100 if parsed.assumed else 0), Stamp(
                parsed.when, parsed.precision_s, "the stream's program date-time", parsed.assumed, True)))
    places = [("", (info.raw.get("format") or {}).get("tags") or {}, 10),
              (" (video track)", _stream_tags(info, info.video.index if info.video else None), 20),
              (" (audio track)", _stream_tags(info, info.audio.index if info.audio else None), 30)]
    keys = list(_TIME_KEYS)
    for where, tags, rank in places:
        for key, value in tags.items():
            name = key.lower()
            if name not in _TIME_KEYS:
                continue
            parsed = parse_timestamp(value, whole_if_zero=name == "creation_time", local_if_no_zone=True)
            if parsed is None:  # e.g. a date without a time of day
                continue
            preference = rank + keys.index(name) + (100 if parsed.assumed else 0)  # a stated zone first
            found.append((preference, Stamp(parsed.when, parsed.precision_s, f"the file's {_TIME_KEYS[name]}{where}",
                                            parsed.assumed)))
    return min(found, key=lambda f: f[0])[1] if found else None


def copy_start(stamp: Stamp, origin_s: float, first_sample_s: float) -> datetime:
    """Real-world time of a working copy's t = 0. ``origin_s``: the source time of that moment (see
    ingest); ``first_sample_s``: the source's start time (the moment an HLS date states)."""
    return stamp.when + timedelta(seconds=origin_s - (first_sample_s if stamp.at_first_sample else 0.0))


def _digits(precision_s: float) -> int:
    return 0 if precision_s >= 1 else 3 if precision_s >= 0.001 else 6


def iso_utc(when: datetime) -> str:
    """2026-03-20T18:21:23.203735Z (no fraction when it is zero): as ffmpeg takes a creation time."""
    text = when.astimezone(timezone.utc).isoformat(timespec="microseconds" if when.microsecond else "seconds")
    return text.replace("+00:00", "Z")


def iso_local(when: datetime, precision_s: float) -> str:
    """2026-03-20T11:37:15-05:00 in this computer's time zone, with the decimals the precision supports
    (cut, as a clock shows a second until the next one begins)."""
    digits = _digits(precision_s)
    local = when.astimezone()
    text = local.strftime("%Y-%m-%dT%H:%M:%S") + (f".{local.microsecond:06d}"[:digits + 1] if digits else "")
    return text + _offset_text(local.utcoffset() or timedelta(0))


def _unix(seconds: float) -> float:
    """Unix time to the microsecond, without a trailing .0 for whole seconds."""
    value = round(seconds, 6)
    return int(value) if value == int(value) else value


def recorded_block(stamp: Optional[Stamp], origin_s: float, first_sample_s: float,
                   duration_s: Optional[float]) -> Dict[str, Any]:
    """session.json's "recorded" for a working copy (see copy_start); ``duration_s``: the end of the
    copy's last frame."""
    if stamp is None:
        return {"start": None, "from": NOT_STATED}
    start = copy_start(stamp, origin_s, first_sample_s)
    source = stamp.source if stamp.source.startswith("entered") else \
        f"{stamp.source}, {_precision_words(stamp.precision_s)}"
    if stamp.assumed_zone:
        source += " (no time zone given: read as this computer's local time)"
    block: Dict[str, Any] = {"start": iso_local(start, stamp.precision_s)}
    if duration_s:
        block["end"] = iso_local(start + timedelta(seconds=duration_s), stamp.precision_s)
    block["start_unix"] = _unix(start.timestamp())
    if duration_s:
        block["end_unix"] = _unix(start.timestamp() + duration_s)
    block["from"] = source
    return block


def upgrade_block(block: Any) -> Dict[str, Any]:
    """A block as 0.2 previews wrote it ({"start_utc", "end_utc", "precision_s", "source", "stated"}) in
    today's form; {} (looked up again) when it found nothing. Other blocks are returned unchanged."""
    if not isinstance(block, dict) or "start_utc" not in block:
        return block if isinstance(block, dict) else {}
    start = parse_timestamp(block.get("start_utc"))
    if start is None:
        return {}
    end = parse_timestamp(block.get("end_utc"))
    precision = block.get("precision_s") if isinstance(block.get("precision_s"), (int, float)) else start.precision_s
    source = str(block.get("source") or "the file").replace("the container's creation_time", "the file's creation time")
    return recorded_block(Stamp(start.when, float(precision), source), 0.0, 0.0,
                          (end.when - start.when).total_seconds() if end else None)


def start_of(block: Any) -> Optional[datetime]:
    """The start of a "recorded" block (None if unknown or unreadable)."""
    if not isinstance(block, dict):
        return None
    value = block.get("start_unix")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    parsed = parse_timestamp(block.get("start"))
    return parsed.when if parsed else None


def end_of(block: Any) -> Optional[datetime]:
    if not isinstance(block, dict):
        return None
    value = block.get("end_unix")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    parsed = parse_timestamp(block.get("end"))
    return parsed.when if parsed else None


def precision_of(block: Any) -> float:
    """How precise a block's times are, from the decimals of its start (1.0 for whole seconds)."""
    parsed = parse_timestamp(block.get("start")) if isinstance(block, dict) else None
    return parsed.precision_s if parsed else 1.0


def local_text(when: datetime, precision_s: Any, date: bool = True) -> str:
    """``when`` in this computer's time zone: 2026-03-20 13:21:23.204 (milliseconds only when the
    source states fractions of a second)."""
    local = when.astimezone()
    try:
        fraction = float(precision_s) < 1
    except (TypeError, ValueError):
        fraction = False
    text = local.strftime("%Y-%m-%d %H:%M:%S" if date else "%H:%M:%S")
    return text + (f".{local.microsecond // 1000:03d}" if fraction else "")


def utc_offset_text(when: datetime, zone: Optional[tzinfo] = None) -> str:
    """UTC−05:00 for this computer's time zone (or ``zone``) at ``when``."""
    return "UTC" + _offset_text(when.astimezone(zone).utcoffset() or timedelta(0)).replace("-", "−")
