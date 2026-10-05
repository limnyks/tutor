"""Lesson summary check, Sunday test and reviews."""

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tutor.memory.catalog import load_catalog
from tutor.memory.review import open_lessons, review, stop_decision, weekly_test

MEMORY_REPO = Path(__file__).parent / "fixtures" / "memory"
NOW = datetime(2026, 10, 11, 15, tzinfo=timezone.utc)


def ev(type_, course, data, hours_ago, **extra):
    return {"id": f"{type_}{hours_ago}", "ts": (NOW - timedelta(hours=hours_ago)).isoformat(), "type": type_,
            "course": course, "data": data, **extra}


def transcript(tmp_path, text):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "message": {"role": "user", "content": "start lesson"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
    ]))
    return str(p)


def test_stop_blocks_only_when_ending_a_lesson_without_summary(tmp_path):
    catalog = load_catalog(MEMORY_REPO)
    log = [ev("answer", "probability", {"topic": "bayes", "correct": False}, 1)]
    end = {"transcript_path": transcript(tmp_path, "дякую, на сьогодні все")}
    d = stop_decision(end, log, catalog, NOW)
    assert d["decision"] == "block" and "STAT2100" in d["reason"] and "log_summary" in d["reason"]
    assert stop_decision({**end, "stop_hook_active": True}, log, catalog, NOW) is None   # never loops
    mid = {"transcript_path": transcript(tmp_path, "what is the denominator here?")}
    assert stop_decision(mid, log, catalog, NOW) is None                                # mid-lesson
    done = log + [ev("summary", "probability", {"topics": ["bayes"]}, 0)]
    assert stop_decision(end, done, catalog, NOW) is None                               # summary given
    assert open_lessons([ev("answer", "probability", {}, 30)], NOW) == {}               # old answers


def test_weekly_test_covers_this_weeks_topics_and_mistakes():
    catalog = load_catalog(MEMORY_REPO)
    log = [ev("answer", "probability", {"topic": "bayes", "correct": False, "error": "swapped"}, 48),
           ev("study", "linear-algebra", {"topics": ["eigen"] if "eigen" in {t.id for t in catalog["linear-algebra"].topics}
                                          else [catalog["linear-algebra"].topics[0].id]}, 24),
           ev("answer", "probability", {"topic": "axioms", "correct": True}, 24 * 20)]
    t = weekly_test(catalog, log, NOW.date())
    by_course = {c["course"]: c["items"] for c in t["courses"]}
    bayes = [i for i in by_course["STAT2100"] if i["topic"] == "bayes"][0]
    assert bayes["questions"] == 3 and "swapped" in bayes["why"]
    assert "MATH252" in by_course and "source='test'" in t["rules"]


def test_week_review_compares_plan_with_logged_work():
    catalog = load_catalog(MEMORY_REPO)
    day = NOW.date() - timedelta(days=2)
    block = lambda course, h: {"start": f"{day}T{h:02d}:00:00+03:00", "end": f"{day}T{h + 2:02d}:00:00+03:00",
                               "course": course, "kind": "lesson", "title": f"{course}: lesson"}
    log = [ev("plan", None, {"blocks": [block("MATH252", 9), block("CS310", 12)]}, 24 * 3),
           ev("answer", "databases", {"topic": catalog["databases"].topics[0].id, "correct": False,
                                      "error": "forgot GROUP BY"}, 24 * 2 - 2),
           ev("new_grade", None, {"item": "Quiz 4", "grade": "1.50", "range": "0–2"}, 30, course_id=4219)]
    r = review(catalog, log, None, NOW.date(), 7)
    assert r["courses"]["MATH252"]["planned_days_with_no_work_logged"] == 1
    assert r["courses"]["CS310"]["planned_days_with_no_work_logged"] == 0
    assert r["courses"]["CS310"]["mistakes"] == ["forgot GROUP BY"]
    assert r["moodle"]["graded_points_lost"] == ["MATH252: Quiz 4"]
    assert "1.50" not in json.dumps(r)
