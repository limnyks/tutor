"""Memory: catalog, event log, knowledge, briefing, tools and git sync."""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tutor.memory import events as event_log
from tutor.memory import tools
from tutor.memory.briefing import build_briefing
from tutor.memory.catalog import find_course, load_catalog
from tutor.memory.gitsync import publish_snapshot, sync
from tutor.memory.knowledge import build
from tutor.moodle.config import Config

# A copy of the course catalog from the tutor-memory repo.
MEMORY_REPO = Path(__file__).parent / "fixtures" / "memory"


@pytest.fixture
def mem(tmp_path, monkeypatch):
    memory_dir = tmp_path / "tutor-memory"
    shutil.copytree(MEMORY_REPO / "courses", memory_dir / "courses")
    cfg = Config(home=tmp_path / "home", files_dir=tmp_path / "files", state_dir=memory_dir)
    monkeypatch.setattr(tools, "load_config", lambda: cfg)
    return cfg


def test_catalog_has_the_seven_courses_with_topics():
    catalog = load_catalog(MEMORY_REPO)
    assert {c.code for c in catalog.values()} == {"STAT2100", "MATH252", "MATH115", "CS310", "CS240",
                                                   "SCI400", "CS101"}
    assert sum(c.credits for c in catalog.values()) == 29
    assert find_course(catalog, "math252").slug == "linear-algebra"
    assert find_course(catalog, "4442").code == "STAT2100"
    assert find_course(catalog, "Probability").code == "STAT2100"
    prob = catalog["probability"]
    assert prob.topic("bayes").id == "bayes"
    assert prob.topic("Bayes' theorem").id == "bayes"
    assert prob.topic("clt").id == "limit-theorems"
    with pytest.raises(ValueError, match="Use one of"):
        prob.topic("quantum mechanics")


def test_log_and_read_events(mem):
    tools.log_answer("STAT2100", "bayes", False, error="forgot the denominator")
    tools.log_study("probability", ["bayes", "conditional"], minutes=40, stuck_on="priors")
    evs = event_log.read_all(mem.state_dir)
    assert [e["type"] for e in evs] == ["answer", "study"]
    assert evs[0]["course"] == "probability" and evs[0]["data"]["topic"] == "bayes"
    files = list((mem.state_dir / "events").glob("*.jsonl"))
    assert len(files) == 1 and files[0].name.endswith(f".{event_log.writer_name()}.jsonl")


def test_unknown_course_or_topic_is_refused_not_logged(mem):
    with pytest.raises(ValueError, match="Use one of"):
        tools.log_answer("STAT2100", "string theory", True)
    with pytest.raises(ValueError):
        tools.log_answer("Chemistry", "bayes", True)
    assert event_log.read_all(mem.state_dir) == []


def _event(type_, course, data, ts):
    return {"id": ts, "ts": ts, "source": "t", "type": type_, "course": course, "data": data}


def test_knowledge_reviews_follow_the_evidence():
    course = load_catalog(MEMORY_REPO)["probability"]
    day = lambda d: datetime(2026, 10, d, 12, tzinfo=timezone.utc).isoformat()
    log = [
        _event("answer", "probability", {"topic": "bayes", "correct": False, "error": "swapped P(A|B)"}, day(1)),
        _event("answer", "probability", {"topic": "bayes", "correct": True}, day(2)),
        _event("answer", "probability", {"topic": "bayes", "correct": True}, day(3)),
        _event("study", "probability", {"topics": ["conditional"], "stuck_on": "independence"}, day(3)),
        _event("answer", "linear-algebra", {"topic": "bayes", "correct": True}, day(3)),  # other course
    ]
    states = build(course, log)
    bayes = states["bayes"]
    assert bayes.streak == 2 and len(bayes.answers) == 3
    assert bayes.next_review().isoformat() == "2026-10-06"  # 2 right in a row -> 3 days
    assert not bayes.review_due(datetime(2026, 10, 5).date())
    assert bayes.review_due(datetime(2026, 10, 6).date())
    assert "3 answers, 2 right; last right on 2026-10-03" in bayes.facts()
    assert states["conditional"].unchecked_study and "stuck on: independence" in states["conditional"].facts()
    assert states["axioms"].facts() == "no answers yet"
    # A wrong answer makes the topic due immediately.
    log.append(_event("answer", "probability", {"topic": "bayes", "correct": False, "error": "x"}, day(4)))
    assert build(course, log)["bayes"].review_due(datetime(2026, 10, 4).date())


def test_briefing_has_deadlines_changes_and_work_due(mem):
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    snap = {"courses": {"4442": {"id": 4442, "name": "STAT2100 Probability"}},
            "assignments": {"1": {"course_id": 4442, "name": "HOMEWORK 1", "url": "https://x/mod/assign/view.php?id=1",
                                  "submission_status": "No submissions have been made yet",
                                  "dates": {"due": {"iso": "2026-10-03T23:59:00+03:00"}}}},
            "quizzes": {}, "deadlines": {}}
    mem.snapshot_path.parent.mkdir(parents=True)
    mem.snapshot_path.write_text(json.dumps(snap))
    mem.status_path.write_text(json.dumps({"result": "ok", "last_success": (now - timedelta(hours=2)).isoformat()}))
    events_dir = mem.state_dir / "events"
    events_dir.mkdir()
    (events_dir / "2026-09-30.mac-moodle.jsonl").write_text(json.dumps(
        {"id": "g", "ts": (now - timedelta(hours=5)).isoformat(), "source": "mac-moodle", "type": "new_grade",
         "course_id": 4219, "course": "MATH252 ...", "data": {"item": "Quiz 4", "grade": "1.50", "range": "0–2"}}) + "\n")
    (events_dir / "2026-09-30.tutor-x.jsonl").write_text("\n".join(json.dumps(e) for e in [
        _event("answer", "probability", {"topic": "bayes", "correct": False, "error": "swapped"}, (now - timedelta(days=1)).isoformat()),
        _event("summary", "probability", {"topics": ["bayes"], "summary": "Bayes flips conditionals.",
                                          "mistakes": ["swapped P(A|B)"]}, (now - timedelta(days=1)).isoformat()),
    ]) + "\n")

    text = build_briefing(mem, now=now)
    assert "Sat 03 Oct 23:59 · STAT2100 · HOMEWORK 1 — No submissions" in text
    assert "MATH252: graded: Quiz 4 — points lost" in text
    assert "1.50" not in text and "0–2" not in text  # feedback, never points
    assert "review due: Total probability theorem and Bayes' theorem" in text
    assert "last lesson" in text and "swapped P(A|B)" in text
    assert "No lessons, answers or study reports recorded yet for: CS240, CS310, MATH115, MATH252." in text
    assert len(text) < 6000  # stays a briefing, not a dump


def test_briefing_without_setup_says_so(tmp_path):
    assert "not set up" in build_briefing(Config(home=tmp_path, state_dir=None))


def test_summary_and_history_tools(mem):
    tools.log_summary("MATH115", ["induction"], "Base case, then step.", ["skipped the base case"], "3 A-level tasks")
    tools.log_answer("MATH115", "induction", True, source="lesson", question="Prove 1+..+n")
    hist = tools.memory_history("MATH115", "induction")
    assert [h["type"] for h in hist] == ["answer", "summary"]
    topics = {t["id"]: t for t in tools.memory_topics("Discrete")}
    assert topics["induction"]["evidence"].startswith("1 answers, 1 right")
    assert "Tutor briefing" in tools.memory_briefing()


def test_publish_snapshot_strips_assignment_conditions(tmp_path):
    publish_snapshot({"assignments": {"1": {"name": "HW", "description": "Solve problem 3"}}}, tmp_path)
    saved = json.loads((tmp_path / "moodle" / "snapshot.json").read_text())
    assert saved["assignments"]["1"] == {"name": "HW"}


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def test_two_writers_sync_through_github_without_conflicts(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    a, b = tmp_path / "mac", tmp_path / "cloud"
    subprocess.run(["git", "clone", "-q", str(remote), str(a)], check=True, capture_output=True)
    shutil.copytree(MEMORY_REPO / "courses", a / "courses")
    assert sync(a, "init") == "synced with GitHub"
    subprocess.run(["git", "clone", "-q", str(remote), str(b)], check=True, capture_output=True)

    monkeypatch.setattr(event_log, "writer_name", lambda: "tutor-mac")
    event_log.append(a, "answer", {"topic": "bayes", "correct": True}, "probability")
    monkeypatch.setattr(event_log, "writer_name", lambda: "tutor-cloud")
    event_log.append(b, "study", {"topics": ["bayes"]}, "probability")

    assert sync(a, "mac") == "synced with GitHub"
    assert sync(b, "cloud") == "synced with GitHub"   # pulls mac's file, pushes its own
    assert sync(a, "mac again") == "synced with GitHub"
    assert {e["type"] for e in event_log.read_all(a)} == {"answer", "study"}
    assert {e["type"] for e in event_log.read_all(b)} == {"answer", "study"}


def test_sync_without_network_keeps_working_locally(tmp_path):
    repo = tmp_path / "m"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    (repo / "courses").mkdir()
    (repo / "courses" / "x.json").write_text("{}")
    outcome = sync(repo)
    assert outcome.startswith("push failed, kept local copy")
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert log.strip()  # committed locally


def test_expected_tool_errors_reach_claude_with_their_message(mem):
    from mcp.server.mcpserver.exceptions import ToolError
    from tutor.mcp_server import _explained
    with pytest.raises(ToolError, match="Use one of"):
        _explained(tools.log_answer)("STAT2100", "astrology", True)


def test_unknown_course_fields_are_ignored(tmp_path):
    shutil.copytree(MEMORY_REPO / "courses", tmp_path / "courses")
    p = tmp_path / "courses" / "probability.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "field_from_the_future": 1}))
    assert load_catalog(tmp_path)["probability"].code == "STAT2100"


def test_unexpected_errors_reach_claude_with_their_type(mem):
    from mcp.server.mcpserver.exceptions import ToolError
    from tutor.mcp_server import _explained

    def broken():
        raise TypeError("unexpected keyword argument 'x'")
    with pytest.raises(ToolError, match="TypeError: unexpected keyword"):
        _explained(broken)()


def test_checkout_on_another_branch_still_syncs_with_main(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    mac, cloud = tmp_path / "mac", tmp_path / "cloud"
    subprocess.run(["git", "clone", "-q", str(remote), str(mac)], check=True, capture_output=True)
    shutil.copytree(MEMORY_REPO / "courses", mac / "courses")
    assert sync(mac, "init") == "synced with GitHub"
    subprocess.run(["git", "clone", "-q", str(remote), str(cloud)], check=True, capture_output=True)
    _git(cloud, "checkout", "-q", "-b", "claude/some-session")
    monkeypatch.setattr(event_log, "writer_name", lambda: "tutor-cloud")
    event_log.append(cloud, "study", {"topics": ["bayes"]}, "probability")
    assert sync(cloud, "cloud") == "synced with GitHub"
    assert sync(mac, "mac") == "synced with GitHub"
    assert [e["type"] for e in event_log.read_all(mac)] == ["study"]
