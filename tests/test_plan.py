"""The study plan: deadlines first and on time, lessons around busy time, Sunday routine."""

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tutor.memory import tools
from tutor.memory.briefing import build_briefing
from tutor.memory.catalog import load_catalog
from tutor.memory.plan import make_plan
from tutor.moodle.config import Config
from tutor.moodle.dates import KYIV
from tutor.moodle.queries import deadline_title

MEMORY_REPO = Path(__file__).parent / "fixtures" / "memory"
MON = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 8, 0, tzinfo=KYIV)


def _snap(*items):
    snap = {"courses": {"4429": {"id": 4429, "name": "CS310 Databases"},
                        "4219": {"id": 4219, "name": "MATH252 Linear Algebra"},
                        "4410": {"id": 4410, "name": "SCI400 Propaganda"}},
            "assignments": {}, "quizzes": {}, "deadlines": {}}
    for i, (cid, name, due, status) in enumerate(items):
        snap["assignments"][str(i)] = {"course_id": cid, "name": name, "url": f"https://x/mod/assign/view.php?id={i}",
                                       "submission_status": status, "dates": {"due": {"iso": due}}}
    return snap


@pytest.fixture
def mem(tmp_path):
    shutil.copytree(MEMORY_REPO / "courses", tmp_path / "courses")
    return tmp_path


def _at(d, hm):
    h, m = map(int, hm.split(":"))
    return datetime.combine(d, datetime.min.time(), KYIV).replace(hour=h, minute=m)


def _plan(mem, snap=None, busy=(), log=(), start=MON, days=7, now=NOW):
    return make_plan(mem, load_catalog(mem), list(log), snap, list(busy), start, days, now)


def test_deadline_work_is_done_a_day_before_due(mem):
    snap = _snap((4429, "Assignment 1", "2026-10-08T00:00:00+03:00", "No submissions have been made yet"))
    plan = _plan(mem, snap)
    work = [b for b in plan["blocks"] if b["kind"] == "deadline"]
    assert sum(b["minutes"] for b in work) == 180
    assert all(b["end"] <= "2026-10-07T00:00:00+03:00" for b in work)
    assert work[0]["title"] == "CS310: Assignment 1" and "Your own work" in work[0]["details"]
    assert not plan["warnings"]


def test_submitted_work_and_other_courses_are_skipped(mem):
    snap = _snap((4429, "Assignment 1", "2026-10-08T00:00:00+03:00", "Submitted for grading"),
                 (9999, "Grants form", "2026-10-08T00:00:00+03:00", "No submissions have been made yet"))
    assert not [b for b in _plan(mem, snap)["blocks"] if b["kind"] == "deadline"]


def test_blocks_never_overlap_busy_time_or_each_other(mem):
    busy = [{"start": _at(MON + timedelta(days=i), "10:00").isoformat(),
             "end": _at(MON + timedelta(days=i), "18:00").isoformat()} for i in range(7)]
    plan = _plan(mem, _snap((4219, "Lab 1", "2026-10-09T23:59:00+03:00", "No submissions have been made yet")), busy)
    spans = sorted((datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in plan["blocks"])
    for a, b in zip(spans, spans[1:]):
        assert a[1] <= b[0]
    for s, e in spans:
        day_busy = (_at(s.date(), "09:45"), _at(s.date(), "18:15"))  # 15 min gap around busy time
        assert e <= day_busy[0] or s >= day_busy[1]


def test_timetable_classes_are_busy(mem):
    (mem / "plan").mkdir()
    (mem / "plan" / "timetable.json").write_text(json.dumps({"classes": [
        {"course": "CS240", "day": "mon", "start": "09:00", "end": "22:00", "what": "all day"}]}))
    plan = _plan(mem, days=1)
    assert plan["blocks"] == []


def test_class_in_calendar_moves_its_course_first(mem):
    busy = [{"start": _at(MON, "09:00").isoformat(), "end": _at(MON, "10:20").isoformat(), "course": "CS240"}]
    lessons = [b for b in _plan(mem, busy=busy, days=1)["blocks"] if b["kind"] == "lesson"]
    assert lessons[0]["course"] == "CS240" and lessons[0]["start"] >= _at(MON, "10:35").isoformat()


def test_daily_cap_and_one_lesson_per_course_a_day(mem):
    plan = _plan(mem)
    by_day = {}
    for b in plan["blocks"]:
        by_day.setdefault(b["start"][:10], []).append(b)
    for day, blocks in by_day.items():
        assert sum(b["minutes"] for b in blocks) <= 300
        lessons = [b["course"] for b in blocks if b["kind"] == "lesson"]
        assert len(lessons) == len(set(lessons))
    assert {b["course"] for b in plan["blocks"] if b["kind"] == "lesson"} >= {"MATH252", "MATH115", "STAT2100",
                                                                              "CS310", "CS240"}
    assert not any(b["course"] in ("SCI400", "CS101") for b in plan["blocks"] if b["kind"] == "lesson")


def test_sunday_test_review_and_month_review(mem):
    plan = _plan(mem, start=date(2026, 10, 25), days=1, now=datetime(2026, 10, 25, 7, tzinfo=KYIV))
    kinds = [b["kind"] for b in plan["blocks"]]
    assert kinds[:3] == ["weekly_test", "week_review", "month_review"]  # last Sunday of October
    plan = _plan(mem, start=date(2026, 10, 11), days=1, now=datetime(2026, 10, 11, 7, tzinfo=KYIV))
    assert "month_review" not in [b["kind"] for b in plan["blocks"]]


def test_work_that_does_not_fit_is_reported(mem):
    snap = _snap((4429, "Assignment 1", "2026-10-05T20:00:00+03:00", "No submissions have been made yet"))
    busy = [{"start": _at(MON, "08:00").isoformat(), "end": _at(MON, "19:00").isoformat()}]
    plan = _plan(mem, snap, busy)
    assert any("Assignment 1" in w and "did not fit" in w for w in plan["warnings"])


def test_reported_work_reduces_what_is_planned(mem):
    snap = _snap((4429, "Assignment 1", "2026-10-09T00:00:00+03:00", "No submissions have been made yet"))
    log = [{"ts": "2026-10-04T10:00:00+00:00", "type": "study", "course": "databases",
            "data": {"topics": [], "minutes": 150, "assignment": "Строк Assignment 1 спливає",
                     "course_code": "CS310"}}]
    work = [b for b in _plan(mem, snap, log=log)["blocks"] if b["kind"] == "deadline"]
    assert sum(b["minutes"] for b in work) == 30


def test_today_starts_after_now(mem):
    now = datetime(2026, 10, 5, 19, 7, tzinfo=KYIV)
    plan = _plan(mem, now=now, days=1)
    assert all(datetime.fromisoformat(b["start"]) >= now for b in plan["blocks"])
    assert plan["blocks"][0]["start"].startswith("2026-10-05T19:15")


def test_deadline_titles():
    assert deadline_title("Строк Assignment 1 — From Messy Data спливає") == "Assignment 1 — From Messy Data"
    assert deadline_title("Lab 1. Linear transformations is due") == "Lab 1. Linear transformations"
    assert deadline_title("Quiz for week 5 closes") == "Quiz for week 5"


def test_plan_tools_and_briefing(tmp_path, monkeypatch):
    shutil.copytree(MEMORY_REPO / "courses", tmp_path / "m" / "courses")
    cfg = Config(home=tmp_path / "h", files_dir=tmp_path / "f", state_dir=tmp_path / "m")
    monkeypatch.setattr(tools, "load_config", lambda: cfg)
    plan = tools.plan_week([], start=datetime.now(KYIV).date().isoformat(), days=1)
    blocks = [{**b, "event_id": f"ev{i}"} for i, b in enumerate(plan["blocks"])]
    assert tools.plan_save(blocks).startswith("plan saved")
    text = build_briefing(cfg)
    assert "## Today's plan" in text
    if blocks:
        assert blocks[0]["title"] in text
    assert tools.log_study("CS310", [], 60, assignment="Assignment 1").endswith("Assignment 1")


def test_cli_plan_with_busy_file_and_save(tmp_path, monkeypatch, capsys):
    from tutor import cli
    shutil.copytree(MEMORY_REPO / "courses", tmp_path / "m" / "courses")
    cfg = Config(home=tmp_path / "h", files_dir=tmp_path / "f", state_dir=tmp_path / "m")
    monkeypatch.setattr(tools, "load_config", lambda: cfg)
    day = date(2026, 10, 12)
    busy = tmp_path / "busy.json"
    busy.write_text(json.dumps([{"start": _at(day, "09:00").isoformat(), "end": _at(day, "21:00").isoformat()}]))
    assert cli.main(["plan", "--busy", str(busy), "--json", "--start", day.isoformat(), "--days", "1"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(b["start"] >= _at(day, "21:15").isoformat() for b in plan["blocks"])
    blocks = tmp_path / "blocks.json"
    blocks.write_text(json.dumps([{**b, "event_id": "e"} for b in plan["blocks"]]))
    assert cli.main(["plan-save", str(blocks)]) == 0
    assert "plan saved" in capsys.readouterr().out


def test_briefing_in_the_cloud_uses_the_published_snapshot_time(tmp_path):
    shutil.copytree(MEMORY_REPO / "courses", tmp_path / "m" / "courses")
    (tmp_path / "m" / "moodle").mkdir()
    synced = datetime.now(timezone.utc) - timedelta(hours=3)
    (tmp_path / "m" / "moodle" / "snapshot.json").write_text(json.dumps(
        {"synced_at": synced.isoformat(), "courses": {}, "assignments": {}, "quizzes": {}, "deadlines": {}}))
    text = build_briefing(Config(home=tmp_path / "h", state_dir=tmp_path / "m"))
    assert "Moodle data from" in text and "not been synced" not in text


def test_at_most_three_subjects_a_day(mem):
    snap = _snap((4429, "Assignment 1", "2026-10-09T00:00:00+03:00", "No submissions have been made yet"),
                 (4219, "Lab 1", "2026-10-09T23:59:00+03:00", "No submissions have been made yet"),
                 (4410, "Essay", "2026-10-09T23:59:00+03:00", "No submissions have been made yet"))
    plan = _plan(mem, snap)
    by_day = {}
    for b in plan["blocks"]:
        if b["course"]:
            by_day.setdefault(b["start"][:10], set()).add(b["course"])
    assert by_day and all(len(c) <= 3 for c in by_day.values())
    assert not any("more than 3 subjects" in w for w in plan["warnings"])


def test_subject_limit_gives_way_only_to_a_deadline(mem):
    (mem / "plan").mkdir()
    (mem / "plan" / "settings.json").write_text(json.dumps({"max_courses_per_day": 1}))
    snap = _snap((4429, "Assignment 1", "2026-10-06T20:00:00+03:00", "No submissions have been made yet"),
                 (4219, "Lab 1", "2026-10-06T20:00:00+03:00", "No submissions have been made yet"))
    plan = _plan(mem, snap, days=2)
    assert any("more than 1 subjects" in w for w in plan["warnings"])


def test_no_daily_limit_and_breaks_between_blocks(mem):
    (mem / "plan").mkdir()
    (mem / "plan" / "settings.json").write_text(json.dumps({"max_minutes": None, "lesson_minutes": 180}))
    blocks = _plan(mem, days=1)["blocks"]
    assert sum(b["minutes"] for b in blocks) > 300
    spans = [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in blocks]
    assert all(b[0] - a[1] >= timedelta(minutes=15) for a, b in zip(spans, spans[1:]))


def test_failed_cloud_sync_does_not_turn_the_briefing_into_login_needed(tmp_path):
    shutil.copytree(MEMORY_REPO / "courses", tmp_path / "m" / "courses")
    (tmp_path / "m" / "moodle").mkdir()
    (tmp_path / "m" / "moodle" / "snapshot.json").write_text(json.dumps(
        {"synced_at": datetime.now(timezone.utc).isoformat(), "courses": {}, "assignments": {}, "quizzes": {},
         "deadlines": {}}))
    cfg = Config(home=tmp_path / "h", state_dir=tmp_path / "m")
    cfg.status_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.status_path.write_text(json.dumps({"result": "login_required"}))
    text = build_briefing(cfg)
    assert "login needed" not in text.lower() and "Moodle data from" in text
