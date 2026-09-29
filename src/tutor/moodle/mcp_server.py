"""Moodle as a Claude connector (MCP server, stdio).

Answers come from the last sync (fast, no browser). `moodle_sync` and `moodle_download`
open Moodle live. Assignment descriptions are never returned: some courses forbid putting
assignment conditions into an AI prompt, so only titles, dates and statuses are exposed.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from .browser import LoginRequired
from .config import load_config
from .store import read_events, read_json
from .sync import run_download, run_sync

mcp = MCPServer("moodle")


def _cfg():
    return load_config()


def _snapshot() -> dict:
    snap = read_json(_cfg().snapshot_path, None)
    if not snap:
        raise RuntimeError("No Moodle data yet. Run moodle_sync (or `tutor moodle-sync`) first.")
    return snap


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


@mcp.tool()
def moodle_status() -> dict:
    """When Moodle was last synced, whether the login still works, and errors from the last run."""
    cfg = _cfg()
    status = read_json(cfg.status_path, {})
    last = _parse_iso(status.get("last_success"))
    status["hours_since_success"] = (
        round((datetime.now(timezone.utc) - last).total_seconds() / 3600, 1) if last else None
    )
    return status


@mcp.tool()
def moodle_courses() -> list:
    """Enrolled courses (id, name, url)."""
    return list(_snapshot()["courses"].values())


@mcp.tool()
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


@mcp.tool()
def moodle_deadlines(days: int = 14) -> list:
    """Upcoming deadlines in the next `days` days: calendar events plus assignment and quiz due dates."""
    snap = _snapshot()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    out, seen_urls = [], set()

    for ev in snap["deadlines"].values():
        when = _parse_iso(ev.get("when"))
        if when and now <= when <= horizon:
            out.append({"when": ev["when"], "name": ev["name"], "course": ev.get("course"),
                        "kind": ev.get("kind"), "url": ev.get("url")})
            seen_urls.add(ev.get("url"))

    for key in ("assignments", "quizzes"):
        for item in snap.get(key, {}).values():
            if item.get("url") in seen_urls:
                continue
            dates = item.get("dates") or {}
            due = (dates.get("due") or dates.get("closes") or {}).get("iso")
            when = _parse_iso(due)
            if when and now <= when <= horizon:
                course = snap["courses"].get(str(item["course_id"]), {})
                out.append({"when": due, "name": item["name"], "course": course.get("name"),
                            "kind": key[:-1], "url": item.get("url"),
                            "submission_status": item.get("submission_status")})
    return sorted(out, key=lambda e: e["when"])


@mcp.tool()
def moodle_grades(course: Optional[str] = None) -> dict:
    """Grade items with points, range, percentage and teacher feedback. All courses if none given."""
    snap = _snapshot()
    courses = [_find_course(snap, course)] if course else list(snap["courses"].values())
    return {
        c["name"]: list(snap["grades"].get(str(c["id"]), {}).values())
        for c in courses
    }


@mcp.tool()
def moodle_assignments(course: Optional[str] = None, include_closed: bool = False) -> list:
    """Assignments and quizzes: name, dates, submission and grading status, grade.

    Descriptions and task conditions are deliberately not included.
    """
    snap = _snapshot()
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
    return sorted(out, key=lambda i: i["due"] or "9999")


@mcp.tool()
def moodle_files(course: Optional[str] = None, query: Optional[str] = None) -> list:
    """Downloaded course files with their local paths. Filter by course and/or name fragment.

    Files marked graded=true belong to graded work: do not read them into an AI prompt.
    """
    cfg = _cfg()
    manifest = read_json(cfg.manifest_path, {})
    only = _find_course(_snapshot(), course)["id"] if course else None
    q = (query or "").lower()
    out = []
    for entry in manifest.values():
        if only is not None and entry["course_id"] != only:
            continue
        if q and q not in entry["path"].lower() and q not in entry["activity"].lower():
            continue
        out.append({k: entry[k] for k in ("path", "activity", "section", "size", "graded", "downloaded_at")})
    return sorted(out, key=lambda e: e["path"])


@mcp.tool()
def moodle_changes(since_hours: int = 72) -> list:
    """What changed on Moodle recently: new activities, files, grades, feedback, deadlines, announcements."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    return [e for e in read_events(_cfg()) if (_parse_iso(e["ts"]) or cutoff) >= cutoff]


@mcp.tool()
def moodle_sync(download: bool = True) -> dict:
    """Read Moodle live now (all courses) and download new files. Takes a few minutes."""
    try:
        return run_sync(_cfg(), download=download, log=lambda _: None)
    except LoginRequired as exc:
        return {"result": "login_required", "message": f"{exc} Ask the user to run: tutor moodle-login"}


@mcp.tool()
def moodle_download(course: str) -> dict:
    """Download new or changed files for one course right now."""
    c = _find_course(_snapshot(), course)
    try:
        events = run_download(_cfg(), c["id"], log=lambda _: None)
    except LoginRequired as exc:
        return {"result": "login_required", "message": f"{exc} Ask the user to run: tutor moodle-login"}
    return {"course": c["name"], "downloaded": [e["data"]["path"] for e in events]}


def main() -> None:
    mcp.run()
