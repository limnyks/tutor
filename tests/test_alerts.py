"""Notifications: once per item, quiet hours, batching, and feedback wording (never points)."""

import re
from datetime import datetime, timedelta, timezone

from tutor.moodle.alerts import in_quiet_hours, send_alerts
from tutor.moodle.config import Config
from tutor.moodle.queries import points_lost

NOON = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)  # 12:00 Kyiv


def _cfg(tmp_path, quiet=None):
    cfg = Config(home=tmp_path, files_dir=tmp_path / "f", notify_quiet_hours=quiet)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _snap(due, status="No submissions have been made yet"):
    return {"courses": {"4442": {"id": 4442, "name": "STAT2100 Probability"}},
            "assignments": {"1": {"course_id": 4442, "course": "STAT2100 Probability", "name": "HOMEWORK 1",
                                  "url": "https://x/mod/assign/view.php?id=1", "submission_status": status,
                                  "dates": {"due": {"iso": due.isoformat()}}}},
            "quizzes": {}, "deadlines": {}}


def _grade(i, grade, rng, pct=None):
    return {"id": f"g{i}", "type": "new_grade", "course": "STAT2100 Probability",
            "data": {"item": f"Quiz {i}", "grade": grade, "range": rng, "percentage": pct}}


def test_points_lost_without_showing_points():
    assert points_lost({"grade": "9.00", "range": "0–10"}) is True
    assert points_lost({"grade": "10.00", "range": "0–10"}) is False
    assert points_lost({"percentage": "95.00 %"}) is True
    assert points_lost({"grade": "9,50 / 10,00"}) is True
    assert points_lost({"grade": "5.00 / 5.00"}) is False
    assert points_lost({"grade": "9.00"}) is None          # KSE's report has no range column
    assert points_lost({"grade": "-", "range": "0–10"}) is None


def test_grade_alerts_are_feedback_with_no_numbers(tmp_path):
    got = []
    send_alerts(_cfg(tmp_path), [_grade(1, "7.50", "0–10"), _grade(2, "10.00", "0–10")], {}, NOON,
                send=lambda t, m: got.append(m))
    assert got[0] == "STAT2100: Quiz 1: points lost — not enough for your target; ask the tutor what to fix"
    assert got[1] == "STAT2100: Quiz 2: nothing lost"
    assert not any(re.search(r"\d[.,]\d|/\d|%", m) for m in got)


def test_deadline_alert_once_per_threshold_and_not_when_submitted(tmp_path):
    cfg, got = _cfg(tmp_path), []
    snap = _snap(NOON + timedelta(hours=40))
    send = lambda t, m: got.append(m)
    send_alerts(cfg, [], snap, NOON, send=send)
    send_alerts(cfg, [], snap, NOON + timedelta(hours=1), send=send)
    assert len(got) == 1 and "HOMEWORK 1 — due" in got[0]
    send_alerts(cfg, [], snap, NOON + timedelta(hours=20), send=send)  # now under 24 h
    assert len(got) == 2
    got.clear()
    send_alerts(_cfg(tmp_path / "b"), [], _snap(NOON + timedelta(hours=10), "Submitted for grading"), NOON,
                send=send)
    assert got == []


def test_quiet_hours_hold_alerts_until_morning(tmp_path):
    cfg, got = _cfg(tmp_path, quiet=[23, 8]), []
    night = datetime(2026, 10, 1, 1).astimezone()     # 01:00 local
    morning = datetime(2026, 10, 1, 9).astimezone()
    send_alerts(cfg, [_grade(1, "5", "0–10")], {}, night, send=lambda t, m: got.append(m))
    assert got == []
    send_alerts(cfg, [], {}, morning, send=lambda t, m: got.append(m))
    send_alerts(cfg, [], {}, morning + timedelta(hours=3), send=lambda t, m: got.append(m))
    assert len(got) == 1 and "Quiz 1" in got[0]


def test_many_changes_are_batched(tmp_path):
    got = []
    send_alerts(_cfg(tmp_path), [_grade(i, "1", "0–2") for i in range(6)], {}, NOON,
                send=lambda t, m: got.append(m))
    assert len(got) == 3 and got[-1] == "4 more Moodle changes — ask the tutor what changed"


def test_in_quiet_hours_wraps_midnight():
    local = lambda h: datetime(2026, 10, 1, h).astimezone()
    assert in_quiet_hours(local(23), [23, 8]) and in_quiet_hours(local(3), [23, 8])
    assert not in_quiet_hours(local(12), [23, 8]) and not in_quiet_hours(local(3), None)
