"""Moodle as a Claude connector (MCP server, stdio).

Answers come from the last sync (fast, no browser). `moodle_sync` and `moodle_download`
open Moodle live. Assignment descriptions are returned only for courses whose AI policy allows
help on homework (`homework_help: true` in the memory repo's course file); other courses forbid
putting assignment conditions into an AI prompt, so only titles, dates and statuses are exposed.
"""

import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from .browser import LoginRequired
from .config import load_config
from .queries import upcoming_deadlines, points_lost
from .store import read_events, read_json
from .sync import run_download, run_sync

def _cfg():
    return load_config()


def _homework_help_ids(cfg) -> set[str]:
    """Moodle ids of courses whose AI policy allows help on graded homework."""
    if cfg.state_dir is None or not (cfg.state_dir / "courses").exists():
        return set()
    from ..memory.catalog import load_catalog
    return {str(c.moodle_id) for c in load_catalog(cfg.state_dir).values()
            if c.homework_help and c.moodle_id is not None}


def _on_mac(cfg) -> bool:
    """Moodle can be read live only where the login lives (the Mac's Chrome profile)."""
    return cfg.profile_dir.exists() or sys.platform == "darwin"


def _published(cfg, name: str):
    """The Mac's copy in the memory repo (what the cloud sees)."""
    return cfg.state_dir / "moodle" / name if cfg.state_dir else None


def _snapshot() -> dict:
    cfg = _cfg()
    snap = read_json(cfg.snapshot_path, None)
    if not snap and _published(cfg, "snapshot.json"):
        snap = read_json(_published(cfg, "snapshot.json"), None)  # cloud: the Mac's last sync
    if not snap:
        raise RuntimeError("No Moodle data yet. On the Mac: `tutor moodle-sync`.")
    return snap


def _not_here(cfg) -> dict:
    snap = read_json(_published(cfg, "snapshot.json"), {}) if _published(cfg, "snapshot.json") else {}
    return {"result": "not_on_mac",
            "message": "Moodle can only be read from the Mac, where the login is. Do not ask for "
                       "Moodle credentials here. Data available is from the Mac's last sync"
                       + (f" at {snap['synced_at']}." if snap.get("synced_at") else ".")}


def _find_course(snap: dict, query: str) -> dict:
    q = query.strip().lower()
    courses = list(snap["courses"].values())
    for c in courses:
        if str(c["id"]) == q:
            return c
    matches = [c for c in courses if q in c["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    names = [c["name"] for c in (matches or courses)]
    raise ValueError(f"Course '{query}' is ambiguous or unknown. Candidates: {names}")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def moodle_status() -> dict:
    """When Moodle was last synced, whether the login still works, and errors from the last run."""
    cfg = _cfg()
    status = read_json(cfg.status_path, {})
    if not status and not _on_mac(cfg):
        snap = read_json(_published(cfg, "snapshot.json"), {}) if _published(cfg, "snapshot.json") else {}
        status = {"result": "cloud: data from the Mac's last sync", "last_success": snap.get("synced_at")}
    last = _parse_iso(status.get("last_success"))
    status["hours_since_success"] = (
        round((datetime.now(timezone.utc) - last).total_seconds() / 3600, 1) if last else None
    )
    return status


def moodle_courses() -> list:
    """Enrolled courses (id, name, url)."""
    return list(_snapshot()["courses"].values())


def moodle_course_contents(course: str) -> dict:
    """Sections and activities (files, assignments, quizzes, forums…) of one course.

    course: course id or part of its name, e.g. "MATH252" or "Probability".
    """
    snap = _snapshot()
    c = _find_course(snap, course)
    sections: dict = {}
    for act in snap["activities"].values():
        if act["course_id"] == c["id"]:
            sections.setdefault(act["section"] or "General", []).append(
                {"id": act["id"], "kind": act["kind"], "name": act["name"], "url": act["url"]}
            )
    return {"course": c, "synced_at": snap["synced_at"], "sections": sections}


def moodle_deadlines(days: int = 14, overdue_days: int = 7) -> list:
    """Deadlines in the next `days` days: assignments, quizzes and other calendar events.

    Also lists assignments due in the last `overdue_days` days that were not submitted
    (marked overdue=true), since some courses accept late work.
    """
    return upcoming_deadlines(_snapshot(), datetime.now(timezone.utc), days, overdue_days)


def moodle_grades(course: Optional[str] = None) -> dict:
    """Grade items with points, range, percentage, teacher feedback and `points_lost`.
    All courses if none given.

    For the tutor's reasoning only: never quote points, percentages or scales to the student.
    Turn them into feedback: what was lost, why, and what to do differently next time."""
    snap = _snapshot()
    courses = [_find_course(snap, course)] if course else list(snap["courses"].values())
    return {
        c["name"]: [{**g, "points_lost": points_lost(g)} for g in snap["grades"].get(str(c["id"]), {}).values()]
        for c in courses
    }


def moodle_assignments(course: Optional[str] = None, include_closed: bool = False) -> list:
    """Assignments and quizzes: name, dates, submission and grading status, grade.

    The description (task conditions) is included only for courses whose AI policy allows help
    on homework; there, tutor the student through it (they write the work and cite the help).
    """
    snap = _snapshot()
    allowed = _homework_help_ids(_cfg())
    only = _find_course(snap, course)["id"] if course else None
    now = datetime.now(timezone.utc)
    out = []
    for key in ("assignments", "quizzes"):
        for item in snap.get(key, {}).values():
            if only is not None and item["course_id"] != only:
                continue
            dates = item.get("dates") or {}
            due = (dates.get("due") or dates.get("closes") or {}).get("iso")
            when = _parse_iso(due)
            if not include_closed and when and when < now:
                continue
            out.append({
                "kind": key[:-1],
                "course": snap["courses"].get(str(item["course_id"]), {}).get("name"),
                "name": item["name"],
                "dates": {k: v.get("text") for k, v in dates.items()},
                "due": due,
                "submission_status": item.get("submission_status"),
                "grading_status": item.get("grading_status"),
                "grade": item.get("grade"),
                "url": item.get("url"),
            })
            if str(item["course_id"]) in allowed and item.get("description"):
                out[-1]["description"] = item["description"]
    return sorted(out, key=lambda i: i["due"] or "9999")


def moodle_files(course: Optional[str] = None, query: Optional[str] = None) -> list:
    """Downloaded course files with their local paths. Filter by course and/or name fragment.

    Files marked graded=true belong to graded work: read them only when `homework_help` is true
    (the course's AI policy allows help on homework).
    """
    cfg = _cfg()
    if not cfg.manifest_path.exists() and not _on_mac(cfg):
        raise RuntimeError("Course files are only on the Mac (~/Tutor/Moodle); not available in the cloud.")
    allowed = _homework_help_ids(cfg)
    manifest = read_json(cfg.manifest_path, {})
    only = _find_course(_snapshot(), course)["id"] if course else None
    q = (query or "").lower()
    out = []
    for entry in manifest.values():
        if entry.get("skipped"):
            continue
        if only is not None and entry["course_id"] != only:
            continue
        if q and q not in entry["path"].lower() and q not in entry["activity"].lower():
            continue
        out.append({**{k: entry[k] for k in ("path", "activity", "section", "size", "graded", "downloaded_at")},
                    "homework_help": str(entry["course_id"]) in allowed})
    return sorted(out, key=lambda e: e["path"])


def moodle_changes(since_hours: int = 72) -> list:
    """What changed on Moodle recently: new activities, files, grades, feedback, deadlines, announcements."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    cfg = _cfg()
    events = read_events(cfg)
    if not events and cfg.state_dir:  # cloud: the Mac mirrors its Moodle events into the memory repo
        from ..memory.events import read_all
        from ..memory.events import MEMORY_TYPES
        events = [e for e in read_all(cfg.state_dir) if e.get("type") not in MEMORY_TYPES]
    return [e for e in events if (_parse_iso(e.get("ts")) or cutoff) >= cutoff]


def moodle_sync(download: bool = True) -> dict:
    """Read Moodle live now (all courses) and download new files. Takes a few minutes. Mac only."""
    if not _on_mac(_cfg()):
        return _not_here(_cfg())
    try:
        return run_sync(_cfg(), download=download, log=lambda _: None)
    except LoginRequired as exc:
        return {"result": "login_required", "message": f"{exc} Ask the user to run: tutor moodle-login"}


def moodle_download(course: str) -> dict:
    """Download new or changed files for one course right now. Mac only."""
    if not _on_mac(_cfg()):
        return _not_here(_cfg())
    c = _find_course(_snapshot(), course)
    try:
        events = run_download(_cfg(), c["id"], log=lambda _: None)
    except LoginRequired as exc:
        return {"result": "login_required", "message": f"{exc} Ask the user to run: tutor moodle-login"}
    return {"course": c["name"],
            "downloaded": [e["data"]["path"] for e in events if e["data"].get("path")],
            "too_large": [e["data"]["activity"] for e in events if e["type"] == "file_too_large"]}


TOOLS = [moodle_status, moodle_courses, moodle_course_contents, moodle_deadlines, moodle_grades,
         moodle_assignments, moodle_files, moodle_changes, moodle_sync, moodle_download]


def main() -> None:
    from ..mcp_server import make_server

    make_server("moodle", TOOLS).run()
