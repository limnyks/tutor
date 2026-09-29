"""Sync, diff and download logic, run against saved pages through a fake browser session."""

import json
from pathlib import Path

import pytest

from tutor.moodle.browser import LoginRequired
from tutor.moodle.config import Config
from tutor.moodle.files import Fetched
from tutor.moodle.store import read_events, read_json
from tutor.moodle.sync import run_download, run_sync

BASE = "https://teaching.kse.org.ua"
FIX = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIX / name).read_text()


class FakeSession:
    """Stands in for MoodleSession: serves fixture pages and fake files."""

    pages: dict = {}
    files: dict = {}
    login_expired = False

    def __init__(self, cfg):
        self.cfg = cfg

    def __enter__(self):
        if FakeSession.login_expired:
            raise LoginRequired("expired")
        return self

    def __exit__(self, *exc):
        pass

    def get_html(self, path_or_url):
        path = path_or_url.replace(BASE, "")
        return BASE + path, self.pages.get(path, "<html><body></body></html>")

    def head_final_url(self, url):
        return self.files[url][1] if url in self.files else None

    def fetch(self, url):
        body, final = self.files[url]
        return Fetched(body=body, filename=final.rsplit("/", 1)[-1].split("?")[0],
                       content_type="application/pdf", final_url=final)


@pytest.fixture
def cfg(tmp_path):
    FakeSession.login_expired = False
    FakeSession.pages = {
        "/my/courses.php": page("my_courses.html"),
        "/course/view.php?id=57": page("course.html"),
        "/mod/assign/view.php?id=903": page("assignment_en.html"),
        "/mod/folder/view.php?id=902": page("folder.html"),
        "/mod/forum/view.php?id=900": page("forum.html"),
        "/grade/report/user/index.php?id=57": page("grades.html"),
        "/calendar/view.php?view=upcoming": page("upcoming.html"),
    }
    FakeSession.files = {
        f"{BASE}/mod/resource/view.php?id=901&redirect=1": (b"slides-v1", f"{BASE}/pluginfile.php/1/content/1/Lecture1.pdf"),
        f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice%201.pdf?forcedownload=1": (b"p1", f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice 1.pdf"),
        f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice%202.pdf?forcedownload=1": (b"p2", f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice 2.pdf"),
        f"{BASE}/pluginfile.php/777/mod_assign/introattachment/0/HW1.pdf?forcedownload=1": (b"hw", f"{BASE}/pluginfile.php/777/HW1.pdf"),
    }
    return Config(home=tmp_path / "home", files_dir=tmp_path / "files", state_dir=tmp_path / "state",
                  request_delay=0)


def test_first_sync_downloads_files_and_logs_baseline(cfg):
    summary = run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    assert summary["courses"] == 2 and summary["errors"] == 0

    snap = read_json(cfg.snapshot_path, None)
    assert snap["assignments"]["903"]["dates"]["due"]["iso"] == "2026-10-04T23:59:00+03:00"
    assert snap["grades"]["57"]["Homework 1"]["grade"] == "9.00"
    assert set(snap["announcements"]["57"]) == {"55", "54"}

    slides = cfg.files_dir / "STAT2100 Probability for CS" / "Week 1. Sample spaces" / "Lecture1.pdf"
    assert slides.read_bytes() == b"slides-v1"
    manifest = read_json(cfg.manifest_path, {})
    hw = next(v for v in manifest.values() if v["activity"] == "Homework 1")
    assert hw["graded"] is True

    types = [e["type"] for e in read_events(cfg)]
    assert types[0] == "baseline" and types.count("new_file") == 4
    # Mirrored into the tutor-state repo.
    assert list((cfg.state_dir / "events").glob("*.mac-moodle.jsonl"))
    assert read_json(cfg.status_path, {})["result"] == "ok"


def test_second_sync_reports_only_changes(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    before = len(read_events(cfg))

    # Teacher uploads a new version of the slides and grades the midterm.
    FakeSession.files[f"{BASE}/mod/resource/view.php?id=901&redirect=1"] = (
        b"slides-v2", f"{BASE}/pluginfile.php/1/content/2/Lecture1.pdf")
    FakeSession.pages["/grade/report/user/index.php?id=57"] = page("grades.html").replace(
        '<td class="column-grade">-</td>', '<td class="column-grade">27.00</td>')
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)

    new = read_events(cfg)[before:]
    assert sorted(e["type"] for e in new) == ["file_updated", "new_grade"]
    grade = next(e for e in new if e["type"] == "new_grade")
    assert grade["data"]["item"] == "Midterm exam" and grade["data"]["grade"] == "27.00"
    assert grade["course"] == "STAT2100 Probability for CS"

    folder = cfg.files_dir / "STAT2100 Probability for CS" / "Week 1. Sample spaces"
    assert (folder / "Lecture1.pdf").read_bytes() == b"slides-v2"
    assert len(list((folder / ".versions").iterdir())) == 1


def test_unchanged_files_are_not_downloaded_again(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    calls = []
    original = FakeSession.fetch
    FakeSession.fetch = lambda self, url: calls.append(url) or original(self, url)
    try:
        run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    finally:
        FakeSession.fetch = original
    assert calls == []


def test_login_expired_is_recorded(cfg):
    FakeSession.login_expired = True
    with pytest.raises(LoginRequired):
        run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    assert read_json(cfg.status_path, {})["result"] == "login_required"


def test_download_single_course(cfg):
    run_sync(cfg, download=False, log=lambda _: None, session_cls=FakeSession)
    events = run_download(cfg, 57, log=lambda _: None, session_cls=FakeSession)
    assert len(events) == 4


def test_mcp_tools_hide_assignment_description(cfg, monkeypatch):
    from tutor.moodle import mcp_server

    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    monkeypatch.setattr(mcp_server, "_cfg", lambda: cfg)

    assignments = mcp_server.moodle_assignments("Probability", include_closed=True)
    assert assignments[0]["name"] == "Homework 1"
    assert "description" not in json.dumps(assignments)
    assert "Solve problems" not in json.dumps(assignments)

    contents = mcp_server.moodle_course_contents("STAT2100")
    assert "Week 1. Sample spaces" in contents["sections"]
    assert mcp_server.moodle_grades("57")["STAT2100 Probability for CS"][0]["grade"] == "9.00"
    files = mcp_server.moodle_files("Probability", "practice")
    assert [Path(f["path"]).name for f in files] == ["Practice 1.pdf", "Practice 2.pdf"]
    assert mcp_server.moodle_status()["result"] == "ok"

    with pytest.raises(ValueError):
        mcp_server.moodle_course_contents("Chemistry")
