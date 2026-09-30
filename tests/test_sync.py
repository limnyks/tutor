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

    def get_html(self, path_or_url, wait_for=None):
        path = path_or_url.replace(BASE, "")
        return BASE + path, self.pages.get(path, "<html><body></body></html>")

    def head(self, url):
        if url not in self.files:
            return None, None
        body, final = self.files[url]
        return final, len(body)

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
        "/mod/page/view.php?id=905": page("page.html"),
    }
    FakeSession.files = {
        f"{BASE}/mod/resource/view.php?id=901&redirect=1": (b"slides-v1", f"{BASE}/pluginfile.php/1/content/1/Lecture1.pdf"),
        f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice%201.pdf?forcedownload=1": (b"p1", f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice 1.pdf"),
        f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice%202.pdf?forcedownload=1": (b"p2", f"{BASE}/pluginfile.php/888/mod_folder/content/3/Practice 2.pdf"),
        f"{BASE}/pluginfile.php/777/mod_assign/introattachment/0/HW1.pdf?forcedownload=1": (b"hw", f"{BASE}/pluginfile.php/777/HW1.pdf"),
        f"{BASE}/pluginfile.php/999/mod_page/content/2/er-diagram.png": (b"png", f"{BASE}/pluginfile.php/999/mod_page/content/2/er-diagram.png"),
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
    assert types[0] == "baseline" and types.count("new_file") == 6  # 4 files + page text + its image
    lecture = cfg.files_dir / "STAT2100 Probability for CS" / "Week 1. Sample spaces" / "Lecture 1 — Introduction.html"
    assert "A DBMS manages data." in lecture.read_text()
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
    run_sync(cfg, full_files=True, log=lambda _: None, session_cls=FakeSession)

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
    assert len(events) == 6


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


def test_same_file_name_in_two_activities_keeps_both(cfg):
    """Teachers reuse names like 'Lecture.pdf'; neither file may overwrite the other."""
    FakeSession.files[f"{BASE}/mod/resource/view.php?id=901&redirect=1"] = (
        b"week1", f"{BASE}/pluginfile.php/1/content/1/Practice 1.pdf")
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    folder = cfg.files_dir / "STAT2100 Probability for CS" / "Week 1. Sample spaces"
    contents = sorted(p.read_bytes() for p in folder.iterdir() if p.is_file())
    assert sorted(c for c in contents if not c.startswith(b"<!doctype")) == [b"hw", b"p1", b"p2", b"png", b"week1"]


def test_oversized_file_is_reported_once_and_not_downloaded(cfg):
    cfg.max_file_mb = 0
    logged = []
    calls = []
    original = FakeSession.fetch
    FakeSession.fetch = lambda self, url: calls.append(url) or original(self, url)
    try:
        run_sync(cfg, log=logged.append, session_cls=FakeSession)
        run_sync(cfg, full_files=True, log=logged.append, session_cls=FakeSession)
    finally:
        FakeSession.fetch = original
    assert calls == []
    assert not [line for line in logged if "failed" in line]
    too_large = [e for e in read_events(cfg) if e["type"] == "file_too_large"]
    assert len(too_large) == 6


def test_deadlines_merge_calendar_and_assignment_and_show_overdue(cfg, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from tutor.moodle import mcp_server

    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    monkeypatch.setattr(mcp_server, "_cfg", lambda: cfg)
    snap = read_json(cfg.snapshot_path, None)

    # Pretend today is 2 days before HOMEWORK 1 is due.
    due = datetime.fromisoformat(snap["assignments"]["903"]["dates"]["due"]["iso"])
    monkeypatch.setattr(mcp_server, "datetime", _FrozenDatetime(due - timedelta(days=2)))
    names = [d["name"] for d in mcp_server.moodle_deadlines(days=14)]
    assert names.count("Homework 1") == 1 and "Homework 1 is due" not in names

    # Three days after the deadline, still not submitted: shown as overdue.
    monkeypatch.setattr(mcp_server, "datetime", _FrozenDatetime(due + timedelta(days=3)))
    overdue = [d for d in mcp_server.moodle_deadlines(days=14) if d["overdue"]]
    assert [d["name"] for d in overdue] == ["Homework 1"]


class _FrozenDatetime:
    def __init__(self, now):
        from datetime import datetime
        self._now, self._real = now, datetime

    def now(self, tz=None):
        return self._now.astimezone(tz) if tz else self._now

    def fromisoformat(self, value):
        return self._real.fromisoformat(value)


def test_file_downloaded_under_wrong_name_is_renamed_not_duplicated(cfg):
    url = f"{BASE}/mod/resource/view.php?id=901&redirect=1"
    garbled = "Лекція.pdf".encode("utf-8").decode("latin-1")
    FakeSession.files[url] = (b"slides", f"{BASE}/pluginfile.php/1/content/1/{garbled}")
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    FakeSession.files[url] = (b"slides", f"{BASE}/pluginfile.php/1/content/1/Лекція.pdf")
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    folder = cfg.files_dir / "STAT2100 Probability for CS" / "Week 1. Sample spaces"
    names = sorted(p.name for p in folder.iterdir() if p.is_file())
    assert "Лекція.pdf" in names and garbled not in names


def test_existing_garbled_file_names_are_repaired_without_download(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    manifest = read_json(cfg.manifest_path, {})
    key, entry = next((k, v) for k, v in manifest.items() if v["activity"] == "Lecture 1 slides")
    garbled = "Лекція.pdf".encode("utf-8").decode("latin-1")
    bad_path = Path(entry["path"]).with_name(garbled)
    Path(entry["path"]).rename(bad_path)
    del manifest[key]
    manifest[f"901:{garbled}"] = {**entry, "path": str(bad_path)}
    from tutor.moodle.store import write_json
    write_json(cfg.manifest_path, manifest)

    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    fixed = read_json(cfg.manifest_path, {})
    assert "901:Лекція.pdf" in fixed and Path(fixed["901:Лекція.pdf"]["path"]).name == "Лекція.pdf"
    assert Path(fixed["901:Лекція.pdf"]["path"]).exists() and not bad_path.exists()


def test_assignments_closed_long_ago_are_not_reopened(cfg, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from tutor.moodle import sync

    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    snap = read_json(cfg.snapshot_path, None)
    due = datetime.fromisoformat(snap["assignments"]["903"]["dates"]["due"]["iso"])

    opened = []
    original = FakeSession.get_html
    FakeSession.get_html = lambda self, u, wait_for=None: opened.append(u.replace(BASE, "")) or original(self, u, wait_for)
    try:
        # 20 days after the deadline: the page is reused, the grade report is still read.
        monkeypatch.setattr(sync, "datetime", _FrozenDatetime(due + timedelta(days=20)))
        run_sync(cfg, download=False, log=lambda _: None, session_cls=FakeSession)
        assert "/mod/assign/view.php?id=903" not in opened
        assert "/grade/report/user/index.php?id=57" in opened
        assert read_json(cfg.snapshot_path, None)["assignments"]["903"]["name"] == "Homework 1"

        # 3 days after: still opened (late submissions, grading in progress).
        opened.clear()
        monkeypatch.setattr(sync, "datetime", _FrozenDatetime(due + timedelta(days=3)))
        run_sync(cfg, download=False, log=lambda _: None, session_cls=FakeSession)
        assert "/mod/assign/view.php?id=903" in opened
    finally:
        FakeSession.get_html = original


def test_quick_sync_checks_only_new_activities_full_check_daily(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)  # first run: full
    assert read_json(cfg.status_path, {}).get("last_full_file_check")

    # Teacher replaces a known file; a quick sync (full check not due) doesn't look at it.
    url = f"{BASE}/mod/resource/view.php?id=901&redirect=1"
    FakeSession.files[url] = (b"slides-v2", f"{BASE}/pluginfile.php/1/content/2/Lecture1.pdf")
    calls = []
    original = FakeSession.head
    FakeSession.head = lambda self, u: calls.append(u) or original(self, u)
    try:
        run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
        assert calls == []
        # The daily full check (or --full) picks the update up.
        run_sync(cfg, full_files=True, log=lambda _: None, session_cls=FakeSession)
    finally:
        FakeSession.head = original
    assert "file_updated" in [e["type"] for e in read_events(cfg)]


# --- review pass 1 regressions -------------------------------------------------

def test_page_with_moodle_random_ids_is_not_reported_as_updated(cfg):
    """Moodle's JS stamps time-based ids into rendered pages; that is not a change."""
    def rendered(stamp, sesskey):
        return page("page.html").replace(
            "<p>A DBMS", f'<p id="yui_3_18_1_1_{stamp}_8">A DBMS').replace(
            "manages data.", f'manages data. <a href="/course/view.php?id=57&sesskey={sesskey}">x</a>')

    FakeSession.pages["/mod/page/view.php?id=905"] = rendered(1790802617307, "Ab12Cd34")
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    FakeSession.pages["/mod/page/view.php?id=905"] = rendered(1790809999999, "Zz98Yy76")
    before = len(read_events(cfg))
    run_sync(cfg, full_files=True, log=lambda _: None, session_cls=FakeSession)
    assert [e["type"] for e in read_events(cfg)[before:]] == []


def test_no_courses_keeps_last_snapshot_instead_of_reporting_everything_new(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    snap_before = read_json(cfg.snapshot_path, None)
    FakeSession.pages["/my/courses.php"] = "<html><body>Site maintenance</body></html>"
    with pytest.raises(RuntimeError, match="No courses found"):
        run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    assert read_json(cfg.snapshot_path, None) == snap_before
    assert read_json(cfg.status_path, {})["result"] == "error"

    FakeSession.pages["/my/courses.php"] = page("my_courses.html")
    before = len(read_events(cfg))
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    assert [e["type"] for e in read_events(cfg)[before:]] == []
    assert "traceback" not in read_json(cfg.status_path, {})


def test_calendar_failure_keeps_known_deadlines(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    original = FakeSession.get_html

    def broken(self, u, wait_for=None):
        if "calendar" in u:
            raise TimeoutError("calendar did not load")
        return original(self, u, wait_for)

    FakeSession.get_html = broken
    try:
        summary = run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    finally:
        FakeSession.get_html = original
    assert summary["deadlines"] == 2 and summary["errors"] == 1


def test_event_pushed_past_the_10_event_cap_is_not_reported_new_again(cfg):
    from tutor.moodle import sync as sync_mod

    def upcoming(ids_whens):
        return "<html><body>" + "".join(
            f'<div class="event" data-event-id="{i}" data-event-title="E{i}">'
            f'<div class="description"><div class="row"><div>{w}</div></div></div></div>'
            for i, w in ids_whens) + "</body></html>"

    ten = [(i, f"Monday, {i} November 2026, 10:00 AM") for i in range(10, 20)]
    FakeSession.pages["/calendar/view.php?view=upcoming"] = upcoming(ten)
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    # A new earlier event pushes event 19 (Nov 19) off the list...
    FakeSession.pages["/calendar/view.php?view=upcoming"] = upcoming(
        [(5, "Thursday, 5 November 2026, 10:00 AM")] + ten[:9])
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    # ...then comes back.
    FakeSession.pages["/calendar/view.php?view=upcoming"] = upcoming(ten)
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    new_deadlines = [e["data"]["name"] for e in read_events(cfg) if e["type"] == "new_deadline"]
    assert new_deadlines == ["E5"]
    assert sync_mod.UPCOMING_CAP == 10


def test_new_course_is_one_event_not_one_per_activity(cfg):
    FakeSession.pages["/my/courses.php"] = page("my_courses.html").replace(
        'data-course-id="57"', 'data-course-id="57" hidden').replace("id=57", "id=58")
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)  # course 57 not visible yet
    FakeSession.pages["/my/courses.php"] = page("my_courses.html")
    before = len(read_events(cfg))
    run_sync(cfg, download=False, log=lambda _: None, session_cls=FakeSession)
    types = [e["type"] for e in read_events(cfg)[before:]]
    assert types.count("new_course") == 1
    assert "new_activity" not in types and "new_grade" not in types and "new_announcement" not in types


def test_deleted_file_is_downloaded_again_on_full_check(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    slides = cfg.files_dir / "STAT2100 Probability for CS" / "Week 1. Sample spaces" / "Lecture1.pdf"
    slides.unlink()
    run_sync(cfg, full_files=True, log=lambda _: None, session_cls=FakeSession)
    assert slides.read_bytes() == b"slides-v1"


def test_changing_files_dir_downloads_into_the_new_folder(cfg):
    run_sync(cfg, log=lambda _: None, session_cls=FakeSession)
    cfg.files_dir = cfg.files_dir.parent / "drive"
    run_sync(cfg, full_files=True, log=lambda _: None, session_cls=FakeSession)
    assert (cfg.files_dir / "STAT2100 Probability for CS" / "Week 1. Sample spaces" / "Lecture1.pdf").exists()
    paths = [v["path"] for v in read_json(cfg.manifest_path, {}).values() if v.get("path")]
    assert paths and all(p.startswith(str(cfg.files_dir)) for p in paths)
