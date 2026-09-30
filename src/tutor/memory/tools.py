"""Memory tools for Claude. Every write is validated against the course catalog, so the
log only ever holds real courses and topics."""

from datetime import date, datetime, timezone
from typing import Optional

from ..moodle.config import load_config
from . import events as event_log
from .briefing import build_briefing
from .catalog import find_course, load_catalog
from .knowledge import build


def _memory():
    cfg = load_config()
    if cfg.state_dir is None or not (cfg.state_dir / "courses").exists():
        raise RuntimeError("Tutor memory is not set up (run: tutor memory-init).")
    return cfg, cfg.state_dir, load_catalog(cfg.state_dir)


def memory_briefing() -> str:
    """Today's briefing: deadlines, Moodle changes, reviews due, unchecked study, last lessons."""
    cfg, _, _ = _memory()
    return build_briefing(cfg)


def memory_topics(course: str) -> list:
    """Topics of a course with the evidence recorded for each (facts, no scores).

    course: code (MATH252), slug (probability) or part of the name.
    """
    _, mem, catalog = _memory()
    c = find_course(catalog, course)
    today = datetime.now(timezone.utc).date()
    states = build(c, event_log.read_all(mem))
    return [{"id": t.id, "title": t.title, "week": t.week, "evidence": states[t.id].facts(),
             "review_due": states[t.id].review_due(today)} for t in c.topics]


def log_answer(course: str, topic: str, correct: bool, source: str = "lesson",
               question: str = "", error: str = "") -> str:
    """Record one answer the student gave to a checking question.

    source: lesson | test | practice | quiz. error: what exactly was wrong (when wrong).
    """
    _, mem, catalog = _memory()
    c = find_course(catalog, course)
    t = c.topic(topic)
    event_log.append(mem, "answer", {"topic": t.id, "correct": bool(correct), "source": source,
                                     "question": question[:500], "error": error[:300]}, c.slug)
    return f"recorded: {c.code} / {t.title}: {'right' if correct else 'wrong'}"


def log_summary(course: str, topics: list[str], summary: str,
                mistakes: Optional[list[str]] = None, next_step: str = "") -> str:
    """Record the end-of-lesson summary (the student's own words, corrected), the mistakes
    made in the lesson, and the agreed next step."""
    _, mem, catalog = _memory()
    c = find_course(catalog, course)
    ids = [c.topic(t).id for t in topics]
    event_log.append(mem, "summary", {"topics": ids, "summary": summary[:2000],
                                      "mistakes": [m[:300] for m in (mistakes or [])],
                                      "next_step": next_step[:300]}, c.slug)
    return f"summary recorded for {c.code}: {', '.join(ids)}"


def log_study(course: str, topics: list[str], minutes: int = 0, stuck_on: str = "", note: str = "",
              assignment: str = "") -> str:
    """Record self-study the student reports (outside lessons). Not evidence of knowing:
    the topics get a quick check next time.

    assignment: the Moodle assignment/quiz name when the time went into graded work; the minutes
    then count against that deadline's planned work (topics may be empty)."""
    _, mem, catalog = _memory()
    c = find_course(catalog, course)
    ids = [c.topic(t).id for t in topics]
    event_log.append(mem, "study", {"topics": ids, "minutes": int(minutes), "stuck_on": stuck_on[:300],
                                    "note": note[:500], "assignment": assignment[:200],
                                    "course_code": c.code}, c.slug)
    return f"study recorded for {c.code}: {', '.join(ids) or assignment}"


def _plan_days(start: date) -> int:
    """Through the coming Sunday; on Sunday, through next Sunday."""
    return 8 if start.weekday() == 6 else 7 - start.weekday()


def plan_week(busy: list[dict], start: Optional[str] = None, days: Optional[int] = None) -> dict:
    """Propose study blocks: Sunday test/reviews, work on Moodle deadlines, lessons.

    busy: every timed calendar event in the period as {"start": ISO, "end": ISO} (all calendars;
    leave out all-day events and the tutor's own "tutor-plan" events, which get replaced).
    start: YYYY-MM-DD, default today (Kyiv). days: default through the coming Sunday.
    Returns blocks, minutes per course and warnings (work that does not fit)."""
    from ..moodle.dates import KYIV
    from .briefing import load_snapshot
    from .plan import make_plan
    cfg, mem, catalog = _memory()
    now = datetime.now(timezone.utc)
    first = date.fromisoformat(start) if start else now.astimezone(KYIV).date()
    return make_plan(mem, catalog, event_log.read_all(mem), load_snapshot(cfg), busy, first,
                     days or _plan_days(first), now)


def plan_save(blocks: list[dict]) -> str:
    """Record the plan as written to the calendar: the blocks from plan_week, each with the
    calendar `event_id` it got. The briefing shows today's part; the next re-plan replaces it."""
    _, mem, _ = _memory()
    keep = ("start", "end", "kind", "course", "title", "details", "event_id")
    clean = [{k: b.get(k) for k in keep} for b in blocks]
    event_log.append(mem, "plan", {"blocks": clean})
    return f"plan saved: {len(clean)} blocks"


def memory_history(course: str, topic: Optional[str] = None, limit: int = 20) -> list:
    """Past answers, summaries and study reports of a course (optionally one topic), newest first."""
    _, mem, catalog = _memory()
    c = find_course(catalog, course)
    tid = c.topic(topic).id if topic else None
    out = []
    for ev in reversed(event_log.read_all(mem)):
        if ev.get("course") != c.slug or ev["type"] not in event_log.MEMORY_TYPES:
            continue
        d = ev["data"]
        if tid and d.get("topic") != tid and tid not in d.get("topics", []):
            continue
        out.append({"ts": ev["ts"], "type": ev["type"], **d})
        if len(out) >= limit:
            break
    return out


ALL = [memory_briefing, memory_topics, log_answer, log_summary, log_study, memory_history,
       plan_week, plan_save]
