"""The study plan: what to work on, when. Facts in, blocks out; Claude writes them to Google Calendar.

Inputs: Moodle deadlines, the knowledge log (reviews due, topics not learned yet), the class
timetable (<memory>/plan/timetable.json), busy time from the calendar (given by Claude), and the
settings in <memory>/plan/settings.json. Order of filling:
  1. Sunday: weekly test and week review (plus the month review on the last Sunday).
  2. Work on Moodle deadlines, earliest due first, finished a day before the deadline.
  3. Lessons in the remaining time, most-needed course first, one lesson per course a day.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ..moodle.dates import KYIV
from ..moodle.queries import NOT_SUBMITTED, deadline_title, parse_iso, upcoming_deadlines
from .catalog import Course
from .knowledge import build

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MARKER = "tutor-plan"  # in every calendar event the tutor creates; nothing else is ever deleted

DEFAULT_SETTINGS = {
    "term_start": "2026-09-01",
    # When study may be planned, per weekday.
    "window": {"mon": "09:00-22:00", "tue": "09:00-22:00", "wed": "09:00-22:00", "thu": "09:00-22:00",
               "fri": "09:00-22:00", "sat": "10:00-21:00", "sun": "10:00-20:00"},
    # Most study minutes planned per day (lessons + deadline work + Sunday blocks).
    "max_minutes": {"mon": 240, "tue": 240, "wed": 240, "thu": 240, "fri": 240, "sat": 300, "sun": 240},
    "lesson_minutes": 90,
    "min_block_minutes": 30,
    "gap_minutes": 15,            # kept free around busy time and between blocks
    "finish_before_due_hours": 24,
    # Minutes of own work a deadline needs, by kind; "deadlines"-mode courses use the second map.
    "effort": {"assignment": 180, "quiz": 45, "other": 60},
    "effort_deadlines_mode": {"assignment": 90, "quiz": 30, "other": 45},
    # Relative importance of each tutored course, by code.
    "weights": {},
    "weekly_test_minutes": 60,
    "week_review_minutes": 30,
    "month_review_minutes": 45,
}

def load_settings(memory_dir: Path) -> dict:
    path = memory_dir / "plan" / "settings.json"
    raw = json.loads(path.read_text()) if path.exists() else {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in raw.items() if not k.startswith("_")}}


def load_timetable(memory_dir: Path) -> list[dict]:
    """Weekly classes: [{"course": "CS240", "day": "mon", "start": "11:30", "end": "12:50", "what": "lecture"}]."""
    path = memory_dir / "plan" / "timetable.json"
    return json.loads(path.read_text()).get("classes", []) if path.exists() else []


@dataclass
class Block:
    start: datetime
    end: datetime
    kind: str        # deadline | lesson | weekly_test | week_review | month_review
    course: str | None
    title: str
    details: str = ""
    why: str = ""

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "kind": self.kind,
                "course": self.course, "title": self.title, "details": self.details, "why": self.why,
                "minutes": int((self.end - self.start).total_seconds() // 60)}


@dataclass
class Day:
    day: date
    free: list[tuple[datetime, datetime]]
    budget: int
    blocks: list[Block] = field(default_factory=list)
    classes: set[str] = field(default_factory=set)   # course codes with a class that day

    def take(self, minutes: int, not_after: datetime | None = None, min_minutes: int = 30) -> tuple[datetime, datetime] | None:
        """Reserve the earliest free stretch of up to `minutes` (at least min_minutes)."""
        want = min(minutes, self.budget)
        if want < min_minutes:
            return None
        for i, (a, b) in enumerate(self.free):
            if not_after is not None:
                b = min(b, not_after)
            length = int((b - a).total_seconds() // 60)
            if length < min_minutes:
                continue
            got = min(want, length)
            end = a + timedelta(minutes=got)
            self.free[i] = (end, self.free[i][1])
            self.budget -= got
            return a, end
        return None


def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def _subtract(free, busy_start, busy_end):
    out = []
    for a, b in free:
        if busy_end <= a or busy_start >= b:
            out.append((a, b))
            continue
        if busy_start > a:
            out.append((a, busy_start))
        if busy_end < b:
            out.append((busy_end, b))
    return out


def _free_days(start: date, days: int, now: datetime, busy: list[tuple[datetime, datetime]],
               timetable: list[dict], s: dict) -> list[Day]:
    gap = timedelta(minutes=s["gap_minutes"])
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        wd = DAYS[d.weekday()]
        lo, hi = s["window"][wd].split("-")
        a = datetime.combine(d, _hm(lo), KYIV)
        b = datetime.combine(d, _hm(hi), KYIV)
        if now > a:  # today: from the next quarter hour
            nxt = now.astimezone(KYIV) + timedelta(minutes=15 - now.astimezone(KYIV).minute % 15)
            a = max(a, nxt.replace(second=0, microsecond=0))
        free = [(a, b)] if a < b else []
        day = Day(d, free, s["max_minutes"][wd])
        for c in timetable:
            if c.get("day") == wd:
                cs, ce = datetime.combine(d, _hm(c["start"]), KYIV), datetime.combine(d, _hm(c["end"]), KYIV)
                day.free = _subtract(day.free, cs - gap, ce + gap)
                day.classes.add(c.get("course", "").upper())
        for bs, be, course in busy:
            day.free = _subtract(day.free, bs - gap, be + gap)
            if course and bs.astimezone(KYIV).date() == d:
                day.classes.add(course.upper())
        out.append(day)
    return out


def _current_week(term_start: date, today: date) -> int:
    return max(1, (today - term_start).days // 7 + 1)


def _deadline_tasks(snap: dict | None, catalog: dict[str, Course], now: datetime, horizon_days: int,
                    s: dict, done_minutes: dict[str, int]) -> list[dict]:
    if not snap:
        return []
    by_moodle = {str(c.moodle_id): c for c in catalog.values() if c.moodle_id is not None}
    tasks = []
    for d in upcoming_deadlines(snap, now, days=horizon_days + 7, overdue_days=3):
        status = d.get("submission_status") or ""
        if d["kind"] == "assignment" and status and not NOT_SUBMITTED.search(status):
            continue  # submitted
        course = by_moodle.get(str(d.get("course_id")))
        if course is None:
            continue  # ignored or unknown course
        efforts = s["effort_deadlines_mode"] if course.mode == "deadlines" else s["effort"]
        kind = d["kind"] if d["kind"] in ("assignment", "quiz") else "other"
        name = deadline_title(d["name"])
        need = efforts[kind] - done_minutes.get(f"{course.code}:{name}".lower(), 0)
        if need <= 0:
            continue
        tasks.append({"course": course, "name": name, "due": parse_iso(d["when"]), "kind": kind,
                      "need": need, "overdue": d.get("overdue", False), "url": d.get("url")})
    return sorted(tasks, key=lambda t: t["due"])


def _work_done(log: list[dict]) -> dict[str, int]:
    """Minutes the student reported on each assignment (log_study with `assignment`)."""
    out: dict[str, int] = {}
    for ev in log:
        d = ev.get("data", {})
        if ev.get("type") == "study" and d.get("assignment") and d.get("course_code"):
            key = f"{d['course_code']}:{deadline_title(d['assignment'])}".lower()
            out[key] = out.get(key, 0) + int(d.get("minutes") or 0)
    return out


def _course_need(course: Course, log: list[dict], today: date, week: int, weight: float) -> tuple[float, str, str]:
    """(priority, topics for the lesson, why) for one tutored course."""
    states = build(course, log)
    due = [s for s in states.values() if s.review_due(today)]
    unchecked = [s for s in states.values() if s.unchecked_study]
    behind = [t for t in course.topics if (t.week or 99) <= week and not states[t.id].answers]
    nxt = behind[0] if behind else next((t for t in course.topics if not states[t.id].answers), None)
    parts, why = [], []
    if due:
        parts.append("review: " + ", ".join(s.title for s in due[:3]))
        why.append(f"{len(due)} review(s) due")
    if unchecked:
        why.append(f"{len(unchecked)} studied topic(s) not checked yet")
    if len(behind) > 2:
        # Nothing recorded yet for most of the course so far: find out what is known first.
        parts.append(f"diagnostic: short questions on weeks 1–{week} topics, then teach from the first gap")
    elif nxt:
        parts.append(f"learn: {nxt.title}")
    if behind:
        why.append(f"{len(behind)} topic(s) up to week {week} with no answers yet")
    priority = weight * (1 + 0.5 * len(due) + 0.3 * len(unchecked) + 0.4 * min(len(behind), 6))
    return priority, "; ".join(parts), "; ".join(why) or "keep the course moving"


def make_plan(memory_dir: Path, catalog: dict[str, Course], log: list[dict], snap: dict | None,
              busy: list[dict], start: date, days: int, now: datetime) -> dict:
    s = load_settings(memory_dir)
    timetable = load_timetable(memory_dir)
    busy_iv = []
    for b in busy:
        bs, be = parse_iso(b.get("start")), parse_iso(b.get("end"))
        if bs and be and be > bs:
            busy_iv.append((bs if bs.tzinfo else bs.replace(tzinfo=KYIV), be if be.tzinfo else be.replace(tzinfo=KYIV),
                            b.get("course")))
    plan_days = _free_days(start, days, now, busy_iv, timetable, s)
    warnings = []
    if not snap:
        warnings.append("No Moodle data: deadlines are not in this plan.")
    min_block = s["min_block_minutes"]

    # 1. Sunday blocks
    for day in plan_days:
        if day.day.weekday() != 6:
            continue
        items = [("weekly_test", s["weekly_test_minutes"], "Weekly test",
                  "Questions on everything studied this week, all tutored courses; answers are logged."),
                 ("week_review", s["week_review_minutes"], "Week review",
                  "What was planned vs done, what went wrong, next week's plan.")]
        if (day.day + timedelta(days=7)).month != day.day.month:
            items.append(("month_review", s["month_review_minutes"], "Month review",
                          "Deadlines met or missed, Moodle feedback, topics still weak, what to change."))
        for kind, minutes, title, details in items:
            slot = day.take(minutes, min_minutes=minutes)
            if slot:
                day.blocks.append(Block(*slot, kind, None, title, details, "Sunday routine"))
            else:
                warnings.append(f"{day.day:%a %d %b}: no free {minutes} min for the {title.lower()}.")

    # 2. Deadline work, earliest due first
    buffer = timedelta(hours=s["finish_before_due_hours"])
    plan_end = datetime.combine(start + timedelta(days=days), time(0), KYIV)
    for task in _deadline_tasks(snap, catalog, now, days, s, _work_done(log)):
        c = task["course"]
        need = task["need"]
        finish_by = task["due"] - buffer
        if finish_by <= now:
            finish_by = task["due"]  # too late for the buffer: use the time that is left
        if finish_by > plan_end:
            # Due after this plan: do this plan's fair share now, the rest in the next one.
            share = need * (plan_end - now) / (finish_by - now)
            need = min(need, max(min_block, int(-(-share // 15) * 15)))
        # First one block a day (spread out), then any free time left before the deadline.
        for per_day in (1, 99):
            for day in plan_days:
                if need <= 0 or datetime.combine(day.day, time(0), KYIV) >= finish_by:
                    break
                for _ in range(per_day):
                    chunk = min(need, s["lesson_minutes"])
                    slot = day.take(chunk, not_after=finish_by, min_minutes=min(min_block, chunk))
                    if slot is None:
                        break
                    need -= int((slot[1] - slot[0]).total_seconds() // 60)
                    due_txt = task["due"].astimezone(KYIV).strftime("%a %d %b %H:%M")
                    day.blocks.append(Block(*slot, "deadline", c.code, f"{c.code}: {task['name']}",
                                            f"Your own work. Due {due_txt}." + (" OVERDUE." if task["overdue"] else ""),
                                            f"Moodle deadline {due_txt}"))
                    if need <= 0:
                        break
            if need <= 0 or finish_by > plan_end:
                break  # a later deadline gets only its share, spread out
        if need > 0 and finish_by <= plan_end:
            warnings.append(f"{c.code} {task['name']}: {need} min of the estimated work did not fit before "
                            f"{finish_by.astimezone(KYIV):%a %d %b %H:%M}. Free time elsewhere or start now.")

    # 3. Lessons
    term_start = date.fromisoformat(s["term_start"])
    tutored = [c for c in catalog.values() if c.mode == "tutor"]
    planned = {c.code: 0 for c in tutored}   # lessons given to each course so far this plan
    for day in plan_days:
        week = _current_week(term_start, day.day)
        needs = {c.code: _course_need(c, log, day.day, week, float(s["weights"].get(c.code, 1.0)))
                 for c in tutored}
        used = {b.course for b in day.blocks}
        # Lectures are easier to absorb the same day: a small bonus for courses with a class that day.
        # Each lesson already planned halves a course's claim, so no course is left out for the week.
        order = sorted(tutored, key=lambda c: -needs[c.code][0] * (2.0 if c.code in day.classes else 1.0)
                       / (1 + planned[c.code]))
        for c in order:
            if c.code in used:
                continue
            slot = day.take(s["lesson_minutes"], min_minutes=max(min_block, 45))
            if slot is None:
                break
            _, topics, why = needs[c.code]
            day.blocks.append(Block(*slot, "lesson", c.code, f"{c.code}: lesson", topics, why))
            used.add(c.code)
            planned[c.code] += 1

    blocks = sorted((b for d in plan_days for b in d.blocks), key=lambda b: b.start)
    per_course: dict[str, int] = {}
    for b in blocks:
        if b.course:
            per_course[b.course] = per_course.get(b.course, 0) + int((b.end - b.start).total_seconds() // 60)
    return {"from": start.isoformat(), "days": days, "blocks": [b.as_dict() for b in blocks],
            "minutes_per_course": per_course, "warnings": warnings}
