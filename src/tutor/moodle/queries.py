"""Questions answered from the last Moodle snapshot (shared by the Claude tools and the briefing)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_ACTIVITY_URL = re.compile(r"/mod/(\w+)/view\.php\?(?:.*&)?id=(\d+)")
_NOT_SUBMITTED = re.compile(r"no submission|not submitted|no attempt|draft|немає|не надіслано|чернетк", re.IGNORECASE)


def parse_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _activity_key(url: str | None) -> tuple | None:
    m = _ACTIVITY_URL.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def upcoming_deadlines(snap: dict, now: datetime, days: int = 14, overdue_days: int = 7) -> list[dict]:
    """Assignments, quizzes and calendar events due within `days`, plus unsubmitted
    assignments up to `overdue_days` past their deadline (overdue=True)."""
    horizon, past = now + timedelta(days=days), now - timedelta(days=overdue_days)
    out, seen = [], set()

    for key in ("assignments", "quizzes"):
        for item in snap.get(key, {}).values():
            dates = item.get("dates") or {}
            due = (dates.get("due") or dates.get("closes") or {}).get("iso")
            when = parse_iso(due)
            if when is None:
                continue
            status = item.get("submission_status") or ""
            overdue = key == "assignments" and past <= when < now and bool(_NOT_SUBMITTED.search(status))
            if not (now <= when <= horizon or overdue):
                continue
            course = snap["courses"].get(str(item["course_id"]), {})
            out.append({"when": due, "name": item["name"], "course": course.get("name"),
                        "course_id": item["course_id"], "kind": key[:-1], "url": item.get("url"),
                        "submission_status": item.get("submission_status"), "overdue": overdue})
            seen.add(_activity_key(item.get("url")))

    for ev in snap.get("deadlines", {}).values():
        when = parse_iso(ev.get("when"))
        key = _activity_key(ev.get("url"))
        if key is not None and key in seen:
            continue  # same deadline as an assignment/quiz above
        if when and now <= when <= horizon:
            out.append({"when": ev["when"], "name": ev["name"], "course": ev.get("course"),
                        "course_id": ev.get("course_id"), "kind": ev.get("kind"), "url": ev.get("url"),
                        "overdue": False})
    return sorted(out, key=lambda e: e["when"])
