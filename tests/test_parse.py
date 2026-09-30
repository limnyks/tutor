from pathlib import Path

from tutor.moodle.dates import parse_moodle_date
from tutor.moodle.parse import (
    is_login_page,
    oauth_login_url,
    parse_assignment,
    parse_course_page,
    parse_courses,
    parse_file_links,
    parse_forum,
    parse_grade_report,
    parse_upcoming,
)

BASE = "https://teaching.kse.org.ua"
FIX = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIX / name).read_text()


def test_dates_english_and_ukrainian():
    assert parse_moodle_date("Sunday, 4 October 2026, 11:59 PM") == "2026-10-04T23:59:00+03:00"
    assert parse_moodle_date("четвер, 24 вересня 2026, 13:30") == "2026-09-24T13:30:00+03:00"
    assert parse_moodle_date("Monday, 7 December 2026") == "2026-12-07T00:00:00+02:00"
    assert parse_moodle_date("Tuesday, 6 October, 1:30 PM", default_year=2026) == "2026-10-06T13:30:00+03:00"
    assert parse_moodle_date("5 days remaining") is None


def test_courses_skip_site_home_and_hidden_labels():
    courses = parse_courses(page("my_courses.html"), BASE)
    assert [(c["id"], c["name"]) for c in courses] == [
        (41, "MATH252 Applied Linear Algebra"),
        (57, "STAT2100 Probability for CS"),
    ]


def test_course_page_sections_activities_and_section_links():
    acts, sections = parse_course_page(page("course.html"), 57, BASE)
    by_id = {a["id"]: a for a in acts}
    assert set(by_id) == {900, 901, 902, 903, 904, 905}
    assert by_id[901] == {
        "id": 901, "course_id": 57, "section": "Week 1. Sample spaces", "kind": "resource",
        "name": "Lecture 1 slides", "url": f"{BASE}/mod/resource/view.php?id=901",
    }
    assert by_id[900]["section"] == "General"
    assert by_id[903]["kind"] == "assign"
    assert sections == [f"{BASE}/course/view.php?id=57&section=2"]


def test_assignment_english():
    info = parse_assignment(page("assignment_en.html"), BASE)
    assert info["name"] == "Homework 1"
    assert info["dates"]["due"]["iso"] == "2026-10-04T23:59:00+03:00"
    assert info["dates"]["opens"]["iso"] == "2026-09-07T00:00:00+03:00"
    assert info["submission_status"] == "No submissions have been made yet"
    assert info["grading_status"] == "Not graded"
    assert info["files"][0]["name"] == "HW1.pdf"
    assert info["grade"] is None


def test_assignment_ukrainian_with_feedback():
    info = parse_assignment(page("assignment_uk_graded.html"), BASE)
    assert info["dates"]["due"]["iso"] == "2026-09-24T13:30:00+03:00"
    assert info["submission_status"] == "Надіслано для оцінювання"
    assert info["grade"] == "5,50 / 6,00"
    assert info["feedback_comments"] == "Пункт 3: не вказано теорему."


def test_grade_report():
    items = parse_grade_report(page("grades.html"))
    assert items == [
        {"item": "Homework 1", "type": "Assignment", "grade": "9.00", "range": "0–10", "percentage": "90.00 %",
         "feedback": "Problem 4: missing justification.", "average": "7.50"},
        {"item": "Midterm exam", "type": "Quiz", "grade": None, "range": "0–30", "percentage": "-",
         "feedback": None, "average": None},
    ]


def test_upcoming_uses_day_link_and_falls_back_to_text():
    events = {e["id"]: e for e in parse_upcoming(page("upcoming.html"), BASE, default_year=2026)}
    assert events[345]["when"] == "2026-10-04T23:59:00+03:00"
    assert events[345]["course"] == "STAT2100 Probability for CS"
    assert events[345]["url"] == f"{BASE}/mod/assign/view.php?id=903"
    assert events[345]["kind"] == "due"
    assert events[346]["name"] == "Quiz for week 5 closes"
    assert events[346]["when"] == "2026-10-06T13:30:00+03:00"


def test_folder_files_scoped_to_main_region():
    files = parse_file_links(page("folder.html"), BASE, "#region-main")
    assert [f["name"] for f in files] == ["Practice 1.pdf", "Practice 2.pdf"]


def test_forum_discussions_deduplicated():
    posts = parse_forum(page("forum.html"), BASE)
    assert [(p["id"], p["title"]) for p in posts] == [(55, "Midterm moved to 21 October"), (54, "Welcome")]
    assert posts[1]["url"].endswith("discuss.php?d=54")


def test_login_detection():
    assert is_login_page(f"{BASE}/login/index.php", page("login.html"))
    assert is_login_page("https://accounts.google.com/v3/signin/identifier", "")
    assert not is_login_page(f"{BASE}/my/courses.php", page("my_courses.html"))
    assert oauth_login_url(page("login.html"), BASE).startswith(f"{BASE}/auth/oauth2/login.php?id=1")


def test_cyrillic_file_name_from_header_is_repaired():
    from tutor.moodle.browser import _filename, fix_mojibake
    garbled = "Практична_1.pdf".encode("utf-8").decode("latin-1")
    assert fix_mojibake(garbled) == "Практична_1.pdf"
    assert fix_mojibake("Lecture 1.pdf") == "Lecture 1.pdf"
    assert fix_mojibake("Практична.pdf") == "Практична.pdf"
    header = {"content-disposition": f'inline; filename="{garbled}"'}
    assert _filename(header, "https://x/pluginfile.php/1/a.pdf") == "Практична_1.pdf"
