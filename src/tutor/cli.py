"""`tutor` command line."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone

from .moodle import schedule
from .moodle.browser import LoginRequired, MoodleSession, open_login_window
from .moodle.config import config_path, load_config
from .moodle.parse import (
    parse_assignment,
    parse_course_page,
    parse_courses,
    parse_grade_report,
    parse_upcoming,
)
from .moodle.store import AlreadyRunning, read_json


def cmd_login(args) -> int:
    cfg = load_config()
    print("A Chrome window will open on Moodle.")
    print("Sign in with your KSE Google account, check that you see your courses, then quit that Chrome window (Cmd+Q).")
    open_login_window(cfg)
    print("Checking the saved login…")
    try:
        with MoodleSession(cfg) as s:
            _, html = s.get_html("/my/courses.php")
            courses = parse_courses(html, cfg.base_url)
    except LoginRequired as exc:
        print(f"Not logged in: {exc}")
        return 2
    print(f"Logged in. {len(courses)} courses visible.")
    return 0


def cmd_sync(args) -> int:
    from .moodle.sync import run_sync

    cfg = load_config()
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Moodle sync")
    try:
        summary = run_sync(cfg, download=not args.no_download, full_files=True if args.full else None)
    except LoginRequired as exc:
        print(f"Login needed: {exc}\nRun: tutor moodle-login")
        return 2
    except AlreadyRunning as exc:
        print(exc)
        return 0
    print(json.dumps(summary))
    return 0


def cmd_download(args) -> int:
    from .moodle.sync import run_download

    cfg = load_config()
    snap = read_json(cfg.snapshot_path, {})
    matches = [c for c in snap.get("courses", {}).values()
               if args.course == str(c["id"]) or args.course.lower() in c["name"].lower()]
    if len(matches) != 1:
        print(f"Course not found or ambiguous: {[c['name'] for c in matches]}")
        return 1
    events = run_download(cfg, matches[0]["id"])
    print(f"{len(events)} files new or updated.")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    status = read_json(cfg.status_path, None)
    if not status:
        print("Never synced. Run: tutor moodle-login, then tutor moodle-sync")
        return 1
    last = status.get("last_success")
    if last:
        hours = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
        print(f"Last successful sync: {last} ({hours:.1f} h ago)")
    print(f"Last attempt: {status.get('last_attempt')} -> {status.get('result')} {status.get('message', '')}")
    for key in ("courses", "activities", "deadlines", "events", "errors"):
        if key in status:
            print(f"  {key}: {status[key]}")
    return 0 if status.get("result") == "ok" else 2


def cmd_inspect(args) -> int:
    """Save a few real pages and show what the parsers extract, to check them against your Moodle."""
    cfg = load_config()
    out = cfg.data_dir / "inspect"
    out.mkdir(parents=True, exist_ok=True)

    def save(name: str, html: str) -> None:
        (out / f"{name}.html").write_text(html)

    with MoodleSession(cfg) as s:
        _, html = s.get_html("/my/courses.php")
        save("my-courses", html)
        courses = parse_courses(html, cfg.base_url)
        print(f"Courses ({len(courses)}):")
        for c in courses:
            print(f"  {c['id']}: {c['name']}")
        if not courses:
            print(f"No courses parsed. Saved page: {out}/my-courses.html")
            return 1

        course = courses[0]
        if args.course:
            matches = [c for c in courses if args.course.lower() in c["name"].lower() or args.course == str(c["id"])]
            if not matches:
                print(f"\nNo course matches '{args.course}'.")
                return 1
            course = matches[0]
        _, html = s.get_html(course["url"])
        save("course", html)
        acts, sections = parse_course_page(html, course["id"], cfg.base_url)
        print(f"\n{course['name']}: {len(acts)} activities, {len(sections)} extra section pages")
        print("  kinds:", dict(Counter(a["kind"] for a in acts)))
        for a in acts[:8]:
            print(f"  [{a['kind']}] {a['section']} / {a['name']}")

        assign = next((a for a in acts if a["kind"] == "assign"), None)
        if assign:
            _, html = s.get_html(assign["url"])
            save("assignment", html)
            info = parse_assignment(html, cfg.base_url)
            print(f"\nAssignment '{info['name']}': dates={json.dumps(info['dates'], ensure_ascii=False)}")
            print(f"  status={info['submission_status']!r} grading={info['grading_status']!r} files={len(info['files'])}")

        _, html = s.get_html(f"/grade/report/user/index.php?id={course['id']}")
        save("grades", html)
        grades = parse_grade_report(html)
        print(f"\nGrade items: {len(grades)}")
        for g in grades[:6]:
            print(f"  {g['item']}: {g['grade']} ({g['range']})")

        _, html = s.get_html("/calendar/view.php?view=upcoming")
        save("upcoming", html)
        events = parse_upcoming(html, cfg.base_url, default_year=datetime.now().year)
        print(f"\nUpcoming events: {len(events)}")
        for e in events[:6]:
            print(f"  {e['when']}  {e['name']}  ({e['course']})")

    print(f"\nPages saved in {out} (they contain your data; share only what's needed).")
    return 0


def cmd_schedule(args) -> int:
    cfg = load_config()
    if args.action == "install":
        path = schedule.install(cfg)
        print(f"Installed {path}. Syncs at login and every 3 h while awake. Log: {cfg.log_path}")
    elif args.action == "uninstall":
        schedule.uninstall()
        print("Schedule removed.")
    else:
        print(schedule.plist_path().read_text() if schedule.plist_path().exists() else "Not installed.")
    return 0


def cmd_mcp(args) -> int:
    from .moodle.mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    print(f"Config file: {config_path()}{'' if config_path().exists() else ' (not created; using defaults)'}")
    for key in ("base_url", "files_dir", "state_dir", "google_account", "lang", "courses_ignore"):
        print(f"  {key}: {getattr(cfg, key)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tutor")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("moodle-login", help="sign in to Moodle once in Chrome").set_defaults(func=cmd_login)
    p = sub.add_parser("moodle-sync", help="read all courses and download new files")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--full", action="store_true", help="re-check every known file for updates now")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("moodle-download", help="download files for one course now")
    p.add_argument("course", help="course id or part of its name")
    p.set_defaults(func=cmd_download)
    sub.add_parser("moodle-status", help="last sync, login state").set_defaults(func=cmd_status)
    p = sub.add_parser("moodle-inspect", help="check parsers against your real Moodle pages")
    p.add_argument("--course", help="which course to inspect (name fragment)")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("moodle-schedule", help="macOS background sync")
    p.add_argument("action", choices=["install", "uninstall", "show"])
    p.set_defaults(func=cmd_schedule)
    sub.add_parser("moodle-mcp", help="run the Moodle connector (MCP, stdio)").set_defaults(func=cmd_mcp)
    sub.add_parser("config", help="show configuration").set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
