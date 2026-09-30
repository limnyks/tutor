"""A full Moodle sync: read every course, diff against the last snapshot, download files."""

from __future__ import annotations

import re
import traceback
from html import escape
from datetime import datetime
from typing import Callable

from .browser import LoginRequired, MoodleSession, notify
from .config import Config
from .diff import diff_snapshots
from .files import Fetched, repair_names, save_file
from .parse import (
    parse_assignment,
    parse_course_page,
    parse_courses,
    parse_file_links,
    parse_forum,
    page_content,
    parse_grade_report,
    parse_quiz,
    parse_upcoming,
)
from .store import append_events, now_iso, read_json, sync_lock, write_json

Log = Callable[[str], None]

ANNOUNCEMENT_FORUM = re.compile(r"announcement|news|оголошен|новин", re.IGNORECASE)
MAIN_REGION = "#region-main, [role='main']"
_PER_COURSE_KEYS = ("activities", "assignments", "quizzes")


def _ignored(cfg: Config, course: dict) -> bool:
    return any(
        str(course["id"]) == rule or rule.lower() in course["name"].lower()
        for rule in cfg.courses_ignore
    )


def _empty_snapshot() -> dict:
    return {
        "synced_at": now_iso(),
        "courses": {}, "activities": {}, "assignments": {}, "quizzes": {},
        "grades": {}, "deadlines": {}, "announcements": {}, "errors": [],
    }


def _carry_over(old: dict, snap: dict, course_id: int) -> None:
    """Keep last known data for a course that failed to load, so it isn't reported as new later."""
    for key in _PER_COURSE_KEYS:
        for item_id, item in old.get(key, {}).items():
            if item.get("course_id") == course_id:
                snap[key][item_id] = item
    for key in ("grades", "announcements"):
        if str(course_id) in old.get(key, {}):
            snap[key][str(course_id)] = old[key][str(course_id)]


def collect(session: MoodleSession, cfg: Config, old: dict, log: Log) -> dict:
    base = cfg.base_url
    snap = _empty_snapshot()

    _, html = session.get_html("/my/courses.php", wait_for='a[href*="/course/view.php"]')
    courses = parse_courses(html, base)
    if not courses:
        _, html = session.get_html("/my/")
        courses = parse_courses(html, base)
    courses = [c for c in courses if not _ignored(cfg, c)]
    log(f"{len(courses)} courses")

    for course in courses:
        cid = course["id"]
        snap["courses"][str(cid)] = course
        try:
            log(f"  {course['name']}…")
            _collect_course(session, cfg, course, snap, log)
            log(f"  {course['name']}: ok")
        except LoginRequired:
            raise
        except Exception as exc:
            snap["errors"].append({"course_id": cid, "error": repr(exc)})
            _carry_over(old, snap, cid)
            log(f"  {course['name']}: FAILED {exc!r}")

    _, html = session.get_html("/calendar/view.php?view=upcoming")
    for ev in parse_upcoming(html, base, default_year=datetime.now().year):
        snap["deadlines"][str(ev["id"])] = ev
    log(f"{len(snap['deadlines'])} upcoming events")
    return snap


def _collect_course(session: MoodleSession, cfg: Config, course: dict, snap: dict, log: Log = print) -> None:
    base, cid = cfg.base_url, course["id"]
    _, html = session.get_html(course["url"])
    activities, section_urls = parse_course_page(html, cid, base)
    seen = {a["id"] for a in activities}
    for url in section_urls:
        _, html = session.get_html(url)
        for act in parse_course_page(html, cid, base)[0]:
            if act["id"] not in seen:
                activities.append(act)
                seen.add(act["id"])

    to_open = [a for a in activities if a["kind"] in ("assign", "quiz")
               or (a["kind"] == "forum" and ANNOUNCEMENT_FORUM.search(a["name"]))]
    log(f"    {len(activities)} activities, {len(to_open)} pages to open")
    opened = 0
    for act in activities:
        snap["activities"][str(act["id"])] = act
        if act in to_open:
            opened += 1
            log(f"    [{opened}/{len(to_open)}] {act['kind']}: {act['name']}")
        if act["kind"] == "assign":
            _, html = session.get_html(act["url"])
            info = parse_assignment(html, base)
            info.update(course_id=cid, activity_id=act["id"], name=info["name"] or act["name"], url=act["url"])
            snap["assignments"][str(act["id"])] = info
        elif act["kind"] == "quiz":
            _, html = session.get_html(act["url"])
            info = parse_quiz(html)
            info.update(course_id=cid, activity_id=act["id"], name=info["name"] or act["name"], url=act["url"])
            snap["quizzes"][str(act["id"])] = info
        elif act["kind"] == "forum" and ANNOUNCEMENT_FORUM.search(act["name"]):
            _, html = session.get_html(act["url"])
            posts = snap["announcements"].setdefault(str(cid), {})
            for post in parse_forum(html, base):
                posts[str(post["id"])] = post

    _, html = session.get_html(f"/grade/report/user/index.php?id={cid}")
    snap["grades"][str(cid)] = {g["item"]: g for g in parse_grade_report(html)}


def _standalone_html(title: str, content: str) -> bytes:
    return (f'<!doctype html><meta charset="utf-8"><title>{escape(title)}</title>'
            f"<h1>{escape(title)}</h1>\n{content}\n").encode("utf-8")


class _HtmlPage(Exception):
    def __init__(self, html: str):
        self.html = html


def download_files(
    session: MoodleSession, cfg: Config, snap: dict, manifest: dict, log: Log,
    course_id: int | None = None,
) -> list[dict]:
    """Download new or changed files for every resource, folder and assignment attachment."""
    events: list[dict] = []

    def fetch_file(url: str) -> Fetched:
        got = session.fetch(url)
        if got.content_type.startswith("text/html"):
            raise _HtmlPage(got.body.decode("utf-8", "replace"))
        return got

    def save(course, act, url, depth=0):
        try:
            ev = save_file(cfg, manifest, course=course, section=act["section"], activity=act,
                           url=url, fetch=fetch_file, head=session.head)
        except _HtmlPage as page:
            # A resource shown inside a page: download the file(s) it links to.
            if depth == 0:
                for link in parse_file_links(page.html, cfg.base_url, MAIN_REGION):
                    save(course, act, link["url"], depth=1)
            return
        if ev:
            events.append(ev)
            log(f"  {'updated' if ev['type'] == 'file_updated' else 'new'}: {ev['data']['path']}")

    candidates = [a for a in snap["activities"].values()
                  if a["kind"] in ("resource", "folder", "page", "assign")
                  and (course_id is None or a["course_id"] == course_id)]
    log(f"Checking files for {len(candidates)} activities (new files are listed as they download)")
    for act in snap["activities"].values():
        if course_id is not None and act["course_id"] != course_id:
            continue
        course = snap["courses"][str(act["course_id"])]
        try:
            if act["kind"] == "resource":
                sep = "&" if "?" in act["url"] else "?"
                save(course, act, f"{act['url']}{sep}redirect=1")
            elif act["kind"] == "folder":
                _, html = session.get_html(act["url"])
                for link in parse_file_links(html, cfg.base_url, MAIN_REGION):
                    save(course, act, link["url"])
            elif act["kind"] == "page":
                # Lecture notes written directly in Moodle: keep the text, plus files inside it.
                _, html = session.get_html(act["url"])
                content = page_content(html)
                if content:
                    doc = _standalone_html(act["name"], content)
                    ev = save_file(cfg, manifest, course=course, section=act["section"], activity=act,
                                   url=act["url"], fetch=lambda _url, d=doc, a=act: Fetched(
                                       body=d, filename=f"{a['name']}.html",
                                       content_type="text/html", final_url=a["url"]))
                    if ev:
                        events.append(ev)
                        log(f"  page: {ev['data']['path']}")
                for link in parse_file_links(html, cfg.base_url, MAIN_REGION):
                    save(course, act, link["url"])
            elif act["kind"] == "assign":
                for link in snap["assignments"].get(str(act["id"]), {}).get("files", []):
                    save(course, act, link["url"])
        except LoginRequired:
            raise
        except Exception as exc:
            snap["errors"].append({"activity_id": act["id"], "error": repr(exc)})
            log(f"  file download failed for {act['name']}: {exc!r}")
    return events


def write_status(cfg: Config, result: str, message: str = "", **extra) -> None:
    status = read_json(cfg.status_path, {})
    status.update(last_attempt=now_iso(), result=result, message=message, **extra)
    if result == "ok":
        status["last_success"] = status["last_attempt"]
    write_json(cfg.status_path, status)


def run_sync(cfg: Config, *, download: bool = True, log: Log = print, session_cls=MoodleSession) -> dict:
    """Full sync. Returns a summary. Raises LoginRequired when you need to sign in again."""
    with sync_lock(cfg):
        old = read_json(cfg.snapshot_path, None)
        manifest = read_json(cfg.manifest_path, {})
        if repair_names(manifest):
            write_json(cfg.manifest_path, manifest)
        file_events: list[dict] = []
        try:
            with session_cls(cfg) as session:
                snap = collect(session, cfg, old or {}, log)
                if download:
                    try:
                        file_events = download_files(session, cfg, snap, manifest, log)
                    finally:
                        write_json(cfg.manifest_path, manifest)
        except LoginRequired as exc:
            write_status(cfg, "login_required", str(exc))
            notify("Tutor: Moodle login needed", "Run: tutor moodle-login")
            raise
        except Exception as exc:
            write_status(cfg, "error", repr(exc), traceback=traceback.format_exc())
            raise

        events = diff_snapshots(old, snap) + file_events
        write_json(cfg.snapshot_path, snap)
        append_events(cfg, events)
        summary = {
            "courses": len(snap["courses"]),
            "activities": len(snap["activities"]),
            "deadlines": len(snap["deadlines"]),
            "events": len(events),
            "errors": len(snap["errors"]),
        }
        write_status(cfg, "ok", **summary)
        return summary


def run_download(cfg: Config, course_id: int, *, log: Log = print, session_cls=MoodleSession) -> list[dict]:
    """Download files for one course, using the activities from the last sync."""
    with sync_lock(cfg):
        snap = read_json(cfg.snapshot_path, None)
        if not snap:
            raise RuntimeError("No sync yet. Run: tutor moodle-sync")
        manifest = read_json(cfg.manifest_path, {})
        try:
            with session_cls(cfg) as session:
                events = download_files(session, cfg, snap, manifest, log, course_id=course_id)
        finally:
            write_json(cfg.manifest_path, manifest)
        append_events(cfg, events)
        return events
