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
from .moodle.store import AlreadyRunning, read_json, sync_lock


COURSE_LINKS = 'a[href*="/course/view.php"]'


def cmd_login(args) -> int:
    cfg = load_config()
    try:
        # Holding the sync lock keeps a background sync from using the profile meanwhile.
        with sync_lock(cfg):
            print("A Chrome window will open on Moodle.")
            print("Sign in with your KSE Google account, check that you see your courses, "
                  "then quit that Chrome window (Cmd+Q).")
            open_login_window(cfg)
            print("Checking the saved login…")
            with MoodleSession(cfg) as s:
                _, html = s.get_html("/my/courses.php", wait_for=COURSE_LINKS)
                courses = parse_courses(html, cfg.base_url)
    except AlreadyRunning:
        print("A background Moodle sync is running right now. Try again in a few minutes.")
        return 1
    except LoginRequired as exc:
        print(f"Not logged in: {exc}")
        return 2
    print(f"Logged in. {len(courses)} courses visible.")
    return 0


def cmd_sync(args) -> int:
    from .moodle.sync import run_sync

    cfg = load_config()
    _trim_log(cfg.log_path)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Moodle sync", flush=True)
    try:
        summary = run_sync(cfg, download=not args.no_download, full_files=True if args.full else None)
    except LoginRequired as exc:
        print(f"Login needed: {exc}\nRun: tutor moodle-login")
        return 2
    except AlreadyRunning as exc:
        print(exc)
        return 0
    except Exception as exc:  # details are in `tutor moodle-status` / the status file
        print(f"Sync failed: {exc}\nNothing was overwritten; the last good data is kept.")
        return 1
    print(json.dumps(summary))
    return 0


def _trim_log(path, keep_bytes: int = 1_000_000) -> None:
    """Keep the background-sync log from growing forever: keep its last ~1 MB."""
    try:
        if path.stat().st_size > 2 * keep_bytes:
            with open(path, "rb") as fh:
                fh.seek(-keep_bytes, 2)
                tail = fh.read()
            path.write_bytes(tail)
    except OSError:
        pass


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
    if status.get("last_full_file_check"):
        print(f"Last full file check: {status['last_full_file_check']}")
    print(f"Background sync: {'on' if schedule.is_installed() else 'off (tutor moodle-schedule install)'}")
    return 0 if status.get("result") == "ok" else 2


def cmd_inspect(args) -> int:
    """Save a few real pages and show what the parsers extract, to check them against your Moodle."""
    cfg = load_config()
    out = cfg.data_dir / "inspect"
    out.mkdir(parents=True, exist_ok=True)

    def save(name: str, html: str) -> None:
        (out / f"{name}.html").write_text(html)

    try:
        with sync_lock(cfg):
            return _inspect(cfg, args, out, save)
    except AlreadyRunning:
        print("A background Moodle sync is running right now. Try again in a few minutes.")
        return 1


def _inspect(cfg, args, out, save) -> int:
    with MoodleSession(cfg) as s:
        _, html = s.get_html("/my/courses.php", wait_for=COURSE_LINKS)
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


def cmd_memory_init(args) -> int:
    """Point the tutor at a clone of the tutor-memory repo and publish Moodle data into it."""
    from pathlib import Path

    from .memory.gitsync import publish_snapshot, sync
    from .moodle.config import config_path

    target = Path(args.path).expanduser().resolve()
    if not (target / "courses").is_dir():
        print(f"{target} is not a tutor-memory checkout (no courses/ folder).\n"
              f"Clone it first: git clone https://github.com/limnyks/tutor-memory {target}")
        return 1
    path = config_path()
    raw = json.loads(path.read_text()) if path.exists() else {}
    raw["state_dir"] = str(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    cfg = load_config()
    snap = read_json(cfg.snapshot_path, None)
    if snap:
        publish_snapshot(snap, target)
    print(f"Memory: {target}")
    print(f"Sync: {sync(target, 'tutor: set up memory')}")
    print("Start a study session with:  cd ~/tutor-memory && claude")
    return 0


def cmd_briefing(args) -> int:
    from .memory.briefing import build_briefing

    print(build_briefing(load_config()))
    return 0


def cmd_memory_sync(args) -> int:
    from .memory.gitsync import sync

    cfg = load_config()
    if cfg.state_dir is None:
        if not args.quiet:
            print("Memory is not set up (run: tutor memory-init).")
        return 0 if args.quiet else 1
    outcome = sync(cfg.state_dir, "tutor: session")
    if not args.quiet or "failed" in outcome:
        print(f"Memory: {outcome}")
    return 0


def cmd_tutor_mcp(args) -> int:
    from .mcp_server import main as mcp_main

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
    p = sub.add_parser("memory-init", help="use a tutor-memory checkout as the tutor's memory")
    p.add_argument("path", nargs="?", default="~/tutor-memory")
    p.set_defaults(func=cmd_memory_init)
    sub.add_parser("briefing", help="print today's briefing").set_defaults(func=cmd_briefing)
    p = sub.add_parser("memory-sync", help="commit memory and sync it with GitHub")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_memory_sync)
    sub.add_parser("mcp", help="run the tutor connector (Moodle + memory tools, MCP stdio)").set_defaults(
        func=cmd_tutor_mcp)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nStopped. Nothing half-saved; the next run starts from the last complete sync.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
