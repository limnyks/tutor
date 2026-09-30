"""A full Moodle sync: read every course, diff against the last snapshot, download files."""

from __future__ import annotations

import re
import traceback
from html import escape
from datetime import datetime, timedelta, timezone
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

    url, html = session.get_html("/my/courses.php", wait_for='a[href*="/course/view.php"]')
    courses = parse_courses(html, base)
    if not courses:
        url, html = session.get_html("/my/", wait_for='a[href*="/course/view.php"]')
        courses = parse_courses(html, base)
    if not courses:
        # Maintenance page, a policy to accept, a changed layout...: saving this as the new
        # state would make every course look new next time. Keep the last good snapshot.
        raise RuntimeError(f"No courses found on Moodle (page: {url}). Open Moodle in a browser to check.")
    courses = [c for c in courses if not _ignored(cfg, c)]
    log(f"{len(courses)} courses")

    for course in courses:
        cid = course["id"]
        snap["courses"][str(cid)] = course
        try:
            log(f"  {course['name']}…")
            _collect_course(session, cfg, course, snap, log, old)
            log(f"  {course['name']}: ok")
        except LoginRequired:
            raise
        except Exception as exc:
            snap["errors"].append({"course_id": cid, "error": repr(exc)})
            _carry_over(old, snap, cid)
            log(f"  {course['name']}: FAILED {exc!r}")

    try:
        _, html = session.get_html("/calendar/view.php?view=upcoming")
        for ev in parse_upcoming(html, base, default_year=datetime.now().year):
            snap["deadlines"][str(ev["id"])] = ev
        _keep_events_beyond_cap(old, snap)
    except LoginRequired:
        raise
    except Exception as exc:
        snap["errors"].append({"calendar": repr(exc)})
        snap["deadlines"] = dict(old.get("deadlines", {}))
        log(f"  calendar: FAILED {exc!r} (kept the last known deadlines)")
    log(f"{len(snap['deadlines'])} upcoming events")
    return snap


UPCOMING_CAP = 10  # Moodle's "upcoming events" page lists at most this many by default


def _keep_events_beyond_cap(old: dict, snap: dict) -> None:
    """Keep known future events that fell off the end of a full upcoming list.

    When the list is full, a later event pushed out by a new earlier one isn't gone;
    dropping it would report it as new again when it comes back.
    """
    new = snap["deadlines"]
    if len(new) < UPCOMING_CAP:
        return
    whens = [e["when"] for e in new.values() if e.get("when")]
    if not whens:
        return
    last = max(datetime.fromisoformat(w) for w in whens)
    for eid, ev in old.get("deadlines", {}).items():
        if eid not in new and ev.get("when") and datetime.fromisoformat(ev["when"]) > last:
            new[eid] = ev


CLOSED_REUSE_DAYS = 14


def _closed_long_ago(item: dict | None, now: datetime) -> bool:
    """An assignment/quiz whose due/close date is over CLOSED_REUSE_DAYS days past.

    Its page no longer changes in ways we track here; new grades and feedback for it
    still arrive through the course grade report, which is read on every sync.
    """
    if not item:
        return False
    dates = item.get("dates") or {}
    iso = (dates.get("due") or dates.get("closes") or dates.get("cutoff") or {}).get("iso")
    if not iso:
        return False
    return datetime.fromisoformat(iso) < now - timedelta(days=CLOSED_REUSE_DAYS)


def _collect_course(session: MoodleSession, cfg: Config, course: dict, snap: dict,
                    log: Log = print, old: dict | None = None) -> None:
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

    old = old or {}
    now = datetime.now(timezone.utc)
    reused = 0
    for act in activities:
        store = {"assign": "assignments", "quiz": "quizzes"}.get(act["kind"])
        prev = old.get(store, {}).get(str(act["id"])) if store else None
        if prev is not None and _closed_long_ago(prev, now):
            snap[store][str(act["id"])] = prev
            act["_reused"] = True
            reused += 1

    to_open = [a for a in activities if not a.get("_reused") and (a["kind"] in ("assign", "quiz")
               or (a["kind"] == "forum" and ANNOUNCEMENT_FORUM.search(a["name"])))]
    log(f"    {len(activities)} activities, {len(to_open)} pages to open"
        + (f" ({reused} closed ones reused)" if reused else ""))
    opened = 0
    for act in activities:
        if act.pop("_reused", False):
            snap["activities"][str(act["id"])] = act
            continue
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
    course_id: int | None = None, only_new: bool = False,
) -> list[dict]:
    """Download new or changed files for every resource, folder, page and assignment attachment.

    only_new: skip activities whose files were already downloaded (a quick check for
    new material); the full check for updated files runs about once a day.
    """
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
            label = {"file_updated": "updated", "file_too_large": "too large, skipped"}.get(ev["type"], "new")
            log(f"  {label}: {ev['data'].get('path') or ev['data'].get('activity')}")

    have_files = {v.get("activity_id") for v in manifest.values()}
    candidates = [a for a in snap["activities"].values()
                  if a["kind"] in ("resource", "folder", "page", "assign")
                  and (course_id is None or a["course_id"] == course_id)
                  and not (only_new and a["id"] in have_files)]
    log(f"Checking files for {len(candidates)} activities"
        + (" (new ones only; full check once a day)" if only_new else "")
        + " — new files are listed as they download")
    for act in candidates:
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
                        log(f"  page: {ev['data'].get('path') or ev['data'].get('activity')}")
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
        status.pop("traceback", None)
    write_json(cfg.status_path, status)


FULL_FILE_CHECK_HOURS = 20


def _full_file_check_due(cfg: Config) -> bool:
    last = read_json(cfg.status_path, {}).get("last_full_file_check")
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=FULL_FILE_CHECK_HOURS)


def run_sync(cfg: Config, *, download: bool = True, full_files: bool | None = None,
             log: Log = print, session_cls=MoodleSession) -> dict:
    """Sync all courses. Returns a summary. Raises LoginRequired when you need to sign in again.

    full_files: re-check every known file for updates (default: once a day); otherwise
    only activities without downloaded files are checked.
    """
    if full_files is None:
        full_files = _full_file_check_due(cfg)
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
                        file_events = download_files(session, cfg, snap, manifest, log, only_new=not full_files)
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
        try:
            from .alerts import send_alerts
            send_alerts(cfg, events, snap, datetime.now(timezone.utc))
        except Exception as exc:  # a failed notification must never fail the sync
            log(f"Notifications failed: {exc}")
        if cfg.state_dir and (cfg.state_dir / "courses").exists():
            from ..memory.gitsync import publish_snapshot, sync as memory_sync
            publish_snapshot(snap, cfg.state_dir)
            log(f"Memory: {memory_sync(cfg.state_dir, 'moodle: sync')}")
        summary = {
            "courses": len(snap["courses"]),
            "activities": len(snap["activities"]),
            "deadlines": len(snap["deadlines"]),
            "events": len(events),
            "errors": len(snap["errors"]),
        }
        if download and full_files:
            summary["full_file_check"] = True
            write_status(cfg, "ok", last_full_file_check=now_iso(), **summary)
        else:
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
