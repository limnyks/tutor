"""The briefing every tutor session starts with: deadlines, what changed, what needs work.

Built from the memory repo (events, courses) and the last Moodle snapshot. Facts only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..moodle.config import Config
from ..moodle.dates import KYIV
from ..moodle.queries import parse_iso, points_lost, upcoming_deadlines
from . import events as event_log
from .catalog import Course, load_catalog
from .knowledge import build

MAX_ITEMS = 8


def load_snapshot(cfg: Config) -> dict | None:
    """The Mac's own snapshot, or the copy the Mac publishes into the memory repo."""
    for path in (cfg.snapshot_path, (cfg.state_dir or Path("/nonexistent")) / "moodle" / "snapshot.json"):
        if path.exists():
            return json.loads(path.read_text())
    return None


def _local(ts: str | None) -> str:
    when = parse_iso(ts)
    return when.astimezone(KYIV).strftime("%a %d %b %H:%M") if when else "?"


def _course_label(catalog: dict[str, Course], moodle_id, fallback: str | None) -> str:
    for c in catalog.values():
        if c.moodle_id is not None and str(c.moodle_id) == str(moodle_id):
            return c.code
    return (fallback or "?")[:40]


def _lost_text(grade: dict) -> str:
    """The student gets feedback, never points: say only whether points were lost."""
    return {True: "points lost (use moodle_grades and the feedback to find out where)",
            False: "nothing lost", None: "check moodle_grades"}[points_lost(grade)]


def build_briefing(cfg: Config, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    memory_dir = cfg.state_dir
    if memory_dir is None or not (memory_dir / "courses").exists():
        return "Tutor memory is not set up yet (run: tutor memory-init)."
    catalog = load_catalog(memory_dir)
    log = event_log.read_all(memory_dir)
    today = now.astimezone(KYIV).date()
    out = [f"# Tutor briefing, {now.astimezone(KYIV):%A %d %B %Y, %H:%M} (Kyiv)", ""]

    # Moodle freshness
    status = json.loads(cfg.status_path.read_text()) if cfg.status_path.exists() else {}
    last = parse_iso(status.get("last_success"))
    if status.get("result") == "login_required":
        out.append("**Moodle login needed** — ask the student to run `tutor moodle-login`.")
    elif last is None:
        out.append("Moodle has not been synced yet.")
    else:
        hours = (now - last).total_seconds() / 3600
        out.append(f"Moodle data from {_local(status['last_success'])}"
                   + (f" — **{hours:.0f} h old**, may be stale" if hours > 24 else ""))
    out.append("")

    # Deadlines
    snap = load_snapshot(cfg)
    out.append("## Deadlines (next 10 days, plus unsubmitted overdue)")
    items = upcoming_deadlines(snap, now, days=10) if snap else []
    if not items:
        out.append("- none found" if snap else "- no Moodle data yet")
    for d in items[:15]:
        label = _course_label(catalog, d.get("course_id"), d.get("course"))
        status_txt = f" — {d['submission_status']}" if d.get("submission_status") else ""
        out.append(f"- {'**OVERDUE** ' if d.get('overdue') else ''}{_local(d['when'])} · {label} · "
                   f"{d['name']}{status_txt}")
    out.append("")

    # Recent Moodle changes (72 h)
    cutoff = (now - timedelta(hours=72)).isoformat()
    recent = [e for e in log if e.get("ts", "") >= cutoff and e.get("type") not in event_log.MEMORY_TYPES
              and e.get("type") != "baseline"]
    if recent:
        out.append("## Moodle changes in the last 3 days")
        for e in recent[-MAX_ITEMS * 2:]:
            label = _course_label(catalog, e.get("course_id"), e.get("course"))
            d = e.get("data", {})
            what = {
                "new_grade": lambda: f"graded: {d.get('item')} — {_lost_text(d)}",
                "grade_changed": lambda: f"grade changed: {d.get('item')} — {_lost_text(d)}",
                "new_feedback": lambda: f"teacher feedback on {d.get('item')}: {str(d.get('feedback'))[:200]}",
                "new_activity": lambda: f"new {d.get('kind')}: {d.get('name')}",
                "new_file": lambda: f"new file: {d.get('name')}" + (" (graded work — not for AI)" if d.get("graded") else ""),
                "file_updated": lambda: f"file updated: {d.get('name')}",
                "new_announcement": lambda: f"announcement: {d.get('title')}",
                "due_date_changed": lambda: f"due date moved: {d.get('name')} → {_local(d.get('new'))}",
                "deadline_changed": lambda: f"deadline moved: {d.get('name')} → {_local(d.get('new'))}",
                "new_deadline": lambda: f"new deadline: {d.get('name')} ({_local(d.get('when'))})",
            }.get(e["type"], lambda: e["type"].replace("_", " "))()
            out.append(f"- {label}: {what}")
        out.append("")

    # Per course: what needs work
    for course in catalog.values():
        if course.mode != "tutor":
            continue
        states = build(course, log)
        due = [s for s in states.values() if s.review_due(today)]
        unchecked = [s for s in states.values() if s.unchecked_study and s not in due]
        summaries = [e for e in log if e.get("type") == "summary" and e.get("course") == course.slug]
        if not (due or unchecked or summaries):
            continue
        out.append(f"## {course.code} {course.name.split(' / ')[0]}")
        for s in due[:MAX_ITEMS]:
            out.append(f"- review due: {s.title} — {s.facts()}")
        for s in unchecked[:MAX_ITEMS]:
            out.append(f"- studied, not yet checked: {s.title} — {s.facts()}")
        if summaries:
            last_sum = summaries[-1]["data"]
            out.append(f"- last lesson ({_local(summaries[-1]['ts'])}): {str(last_sum.get('summary', ''))[:300]}")
            if last_sum.get("mistakes"):
                out.append(f"  mistakes then: {'; '.join(last_sum['mistakes'])[:300]}")
        out.append("")

    untouched = [c.code for c in catalog.values() if c.mode == "tutor"
                 and not any(e.get("course") == c.slug for e in log if e.get("type") in event_log.MEMORY_TYPES)]
    if untouched:
        out.append(f"No lessons, answers or study reports recorded yet for: {', '.join(untouched)}.")
    return "\n".join(out).rstrip() + "\n"
