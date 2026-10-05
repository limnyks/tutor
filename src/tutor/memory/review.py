"""Lesson closure, the Sunday test and the week/month reviews. Facts from the log, never scores."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..moodle.dates import KYIV
from ..moodle.queries import parse_iso, points_lost
from .catalog import Course
from .knowledge import build

OPEN_LESSON_HOURS = 6

_END = re.compile(
    r"\b(bye|goodbye|good night|see you|that'?s all|i'?m done|we'?re done|let'?s stop|stop here|"
    r"end (the )?lesson|finish(ed)? for today|enough for today)\b"
    r"|на сьогодні все|на сьогодні досить|закінчимо|завершимо|завершуємо|бувай|до побачення|"
    r"на сьогодні вистачить|^\s*(все|всьо|кінець|дякую|па)[.!]*\s*$",
    re.IGNORECASE)


def _local_day(ts: str) -> date:
    return (parse_iso(ts) or datetime.now(timezone.utc)).astimezone(KYIV).date()


def open_lessons(log: list[dict], now: datetime, within_hours: int = OPEN_LESSON_HOURS) -> dict[str, str]:
    """Courses with answers logged in the last hours and no summary after them: slug -> last answer ts."""
    last_answer: dict[str, str] = {}
    last_summary: dict[str, str] = {}
    for ev in log:
        course = ev.get("course")
        if ev.get("type") == "answer" and course:
            last_answer[course] = max(last_answer.get(course, ""), ev["ts"])
        elif ev.get("type") == "summary" and course:
            last_summary[course] = max(last_summary.get(course, ""), ev["ts"])
    cutoff = (now - timedelta(hours=within_hours)).isoformat()
    return {c: ts for c, ts in last_answer.items() if ts > last_summary.get(c, "") and ts >= cutoff}


def unfinished_lessons(log: list[dict], now: datetime, days: int = 7) -> dict[str, str]:
    """Like open_lessons, over the last days: lessons that ended without the student's summary."""
    return open_lessons(log, now, within_hours=24 * days)


def _last_user_text(transcript_path: str | None) -> str:
    """The student's last typed message in a Claude Code transcript (JSONL)."""
    if not transcript_path or not Path(transcript_path).exists():
        return ""
    last = ""
    for line in Path(transcript_path).read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            last = content
        elif isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:  # tool results are "user" records too, without text blocks
                last = "\n".join(texts)
    return last


def stop_decision(hook_input: dict, log: list[dict], catalog: dict[str, Course], now: datetime) -> dict | None:
    """Claude Code Stop hook: keep the session going until an open lesson has the student's summary.

    Blocks only when the student is ending (their last message says so) and a lesson has answers
    without a summary; never twice in a row (stop_hook_active)."""
    if hook_input.get("stop_hook_active"):
        return None
    lessons = open_lessons(log, now)
    if not lessons:
        return None
    if not _END.search(_last_user_text(hook_input.get("transcript_path")).strip()):
        return None
    names = ", ".join(catalog[c].code if c in catalog else c for c in lessons)
    return {"decision": "block",
            "reason": f"The lesson ({names}) has no summary yet. Before ending: ask the student to write "
                      f"their own summary of what was learned, correct it, then call log_summary with the "
                      f"topics, the corrected summary, the lesson's mistakes and the next step."}


# ---------- Sunday test ----------

def weekly_test(catalog: dict[str, Course], log: list[dict], today: date, max_questions: int = 24) -> dict:
    """What the Sunday test covers: per tutored course, the topics touched this week, reviews due
    and recent mistakes, with how many questions each gets."""
    week_start = today - timedelta(days=6)
    courses = []
    for c in catalog.values():
        if c.mode != "tutor":
            continue
        states = build(c, log)
        items = []
        for t in c.topics:
            st = states[t.id]
            touched = [ts for ts in [a.ts for a in st.answers] + st.studied + st.covered
                       if week_start <= _local_day(ts) <= today]
            wrong_recent = [a for a in st.answers if not a.correct and week_start <= _local_day(a.ts) <= today]
            due = st.review_due(today)
            if not (touched or due):
                continue
            items.append({"topic": t.id, "title": t.title,
                          "questions": 3 if wrong_recent else 2 if touched else 1,
                          "why": "; ".join(filter(None, [
                              "wrong this week: " + "; ".join(a.error or "wrong" for a in wrong_recent[-2:])
                              if wrong_recent else "",
                              "studied/answered this week" if touched else "",
                              "review due" if due else ""]))})
        if items:
            courses.append({"course": c.code, "items": items})
    total = sum(i["questions"] for c in courses for i in c["items"])
    if total > max_questions:  # trim one-question review items first, then cap per item
        for c in courses:
            c["items"] = [i for i in c["items"] if i["questions"] > 1] or c["items"][:2]
    return {"week": f"{week_start} – {today}", "courses": courses,
            "rules": "Questions in the style of each course's tasks, from its materials; no hints during the "
                     "test; log every answer with log_answer(source='test'); afterwards go through every "
                     "wrong answer: what was wrong and what to do next."}


# ---------- Week / month review ----------

def _plan_for_day(plans: list[dict], day: date) -> list[dict]:
    """The blocks planned for a day: from the last plan saved before that day ended."""
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), KYIV).isoformat()
    chosen = None
    for p in plans:
        if p["ts"] < end:
            chosen = p
    if chosen is None:
        return []
    return [b for b in chosen["data"].get("blocks", []) if _local_day(b["start"]) == day]


def review(catalog: dict[str, Course], log: list[dict], snap: dict | None, today: date, days: int) -> dict:
    """Planned vs done, per course, over the last `days` days. Facts only."""
    start = today - timedelta(days=days - 1)
    plans = sorted((e for e in log if e.get("type") == "plan"), key=lambda e: e["ts"])
    in_period = [e for e in log if start <= _local_day(e["ts"]) <= today]
    by_slug = {c.slug: c for c in catalog.values()}

    planned: dict[str, dict] = {}
    for i in range(days):
        day = start + timedelta(days=i)
        for b in _plan_for_day(plans, day):
            code = b.get("course") or b.get("kind")
            p = planned.setdefault(code, {"blocks": 0, "minutes": 0, "days": set()})
            p["blocks"] += 1
            p["minutes"] += int((parse_iso(b["end"]) - parse_iso(b["start"])).total_seconds() // 60)
            p["days"].add(day)

    courses = {}
    for c in catalog.values():
        evs = [e for e in in_period if e.get("course") == c.slug]
        answers = [e for e in evs if e["type"] == "answer"]
        wrong = [e for e in answers if not e["data"].get("correct")]
        active_days = {_local_day(e["ts"]) for e in evs if e["type"] in ("answer", "study", "summary")}
        p = planned.get(c.code, {"blocks": 0, "minutes": 0, "days": set()})
        entry = {
            "planned_blocks": p["blocks"], "planned_minutes": p["minutes"],
            "planned_days_with_no_work_logged": len(p["days"] - active_days),
            "days_with_work": len(active_days),
            "answers": len(answers), "wrong": len(wrong),
            "mistakes": [e["data"].get("error") for e in wrong if e["data"].get("error")][-5:],
            "lessons_with_summary": sum(1 for e in evs if e["type"] == "summary"),
            "study_minutes_reported": sum(int(e["data"].get("minutes") or 0) for e in evs if e["type"] == "study"),
        }
        if any(v for k, v in entry.items() if k != "mistakes") or entry["mistakes"]:
            courses[c.code] = entry

    moodle = {"graded_points_lost": [], "graded_nothing_lost": [], "feedback": [], "overdue_unsubmitted": []}
    for e in in_period:
        d = e.get("data", {})
        code = next((c.code for c in catalog.values() if c.moodle_id is not None
                     and str(c.moodle_id) == str(e.get("course_id"))), None)
        if code is None:
            continue
        if e.get("type") in ("new_grade", "grade_changed"):
            lost = points_lost(d)
            key = "graded_points_lost" if lost else "graded_nothing_lost" if lost is False else None
            if key:
                moodle[key].append(f"{code}: {d.get('item')}")
        elif e.get("type") == "new_feedback":
            moodle["feedback"].append(f"{code}: {d.get('item')}")
    if snap:
        from ..moodle.queries import upcoming_deadlines
        now = datetime.now(timezone.utc)
        for d in upcoming_deadlines(snap, now, days=0, overdue_days=days):
            if d.get("overdue"):
                moodle["overdue_unsubmitted"].append(f"{d.get('course')}: {d['name']}")

    sunday = planned.get("weekly_test", {}).get("blocks", 0)
    return {"period": f"{start} – {today}", "courses": courses, "moodle": moodle,
            "sunday_blocks_planned": sunday,
            "unfinished_lessons": [by_slug[s].code if s in by_slug else s
                                   for s in unfinished_lessons(log, datetime.now(timezone.utc), days)],
            "how_to_use": "Facts only. Say what was planned and not done, what went wrong and why, and what "
                          "changes next week (settings, weights, block times). No scores, no praise."}
