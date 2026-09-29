"""Parsing Moodle date strings (English or Ukrainian interface) into ISO timestamps."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # Ukrainian, genitive (as Moodle prints dates) and nominative.
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4, "травень": 5, "червень": 6,
    "липень": 7, "серпень": 8, "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
}

_DATE = re.compile(r"(\d{1,2})\s+([^\W\d_]+)(?:\s+(\d{4}))?", re.UNICODE)
_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")


def parse_time(text: str) -> tuple[int, int] | None:
    m = _TIME.search(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return hour, minute


def parse_moodle_date(text: str, default_year: int | None = None) -> str | None:
    """'Sunday, 4 October 2026, 11:59 PM' or 'неділя, 4 жовтня 2026, 23:59' -> ISO string.

    Returns None when no date can be recognised. Dates without a year use default_year.
    Dates without a time are taken as 00:00.
    """
    for m in _DATE.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if not month:
            continue
        year = int(m.group(3)) if m.group(3) else default_year
        if year is None:
            return None
        hm = parse_time(text[m.end():]) or (0, 0)
        return datetime(year, month, int(m.group(1)), hm[0], hm[1], tzinfo=KYIV).isoformat()
    return None


def from_day_timestamp(ts: int, time_text: str) -> str:
    """Combine a calendar day link (unix time) with an 'HH:MM' found in the text."""
    day = datetime.fromtimestamp(ts, KYIV)
    hm = parse_time(time_text)
    if hm:
        day = day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return day.isoformat()
