"""Pure HTML parsers for Moodle 4.x pages.

Each function takes rendered page HTML and returns plain data. No browser here, so
everything is testable against saved pages.
"""

from __future__ import annotations

import copy
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .dates import from_day_timestamp, parse_moodle_date

_HIDDEN_CLASSES = {"sr-only", "accesshide", "visually-hidden"}
_COURSE_LINK = re.compile(r"/course/view\.php\?(?:.*&)?id=(\d+)")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def text_of(tag: Tag | None) -> str:
    """Visible text of a tag, without screen-reader-only spans, whitespace collapsed."""
    if tag is None:
        return ""
    tag = copy.copy(tag)
    for hidden in tag.find_all(True):
        if _HIDDEN_CLASSES.intersection(hidden.get("class") or []):
            hidden.decompose()
    return " ".join(tag.get_text(" ").split())


def _query_int(url: str, key: str) -> int | None:
    values = parse_qs(urlparse(url).query).get(key)
    return int(values[0]) if values and values[0].isdigit() else None


# --- login -----------------------------------------------------------------

def is_login_page(url: str, html: str) -> bool:
    parsed = urlparse(url)
    if (parsed.hostname or "").endswith("accounts.google.com"):
        return True
    if parsed.path.startswith("/login/"):
        return True
    soup = _soup(html)
    body = soup.body
    if body is not None and body.get("id") == "page-login-index":
        return True
    return soup.select_one("form#login, form.login-form") is not None


def oauth_login_url(html: str, base_url: str) -> str | None:
    """The 'Log in with Google' link on Moodle's login page."""
    link = _soup(html).select_one('a[href*="/auth/oauth2/login.php"]')
    return urljoin(base_url, link["href"]) if link else None


# --- courses ---------------------------------------------------------------

def parse_courses(html: str, base_url: str) -> list[dict]:
    """Enrolled courses from /my/courses.php (or any page linking to course/view.php)."""
    courses: dict[int, dict] = {}
    for link in _soup(html).select('a[href*="/course/view.php"]'):
        m = _COURSE_LINK.search(link["href"])
        if not m or "section=" in link["href"]:
            continue
        course_id = int(m.group(1))
        name = text_of(link.select_one(".multiline") or link)
        if course_id == 1 or not name:  # id 1 is the site front page
            continue
        if course_id not in courses or len(name) > len(courses[course_id]["name"]):
            courses[course_id] = {
                "id": course_id,
                "name": name,
                "url": urljoin(base_url, f"/course/view.php?id={course_id}"),
            }
    return sorted(courses.values(), key=lambda c: c["name"])


def _activity_kind(li: Tag) -> str:
    for cls in li.get("class") or []:
        if cls.startswith("modtype_"):
            return cls[len("modtype_"):]
    return "unknown"


def _activity_id(li: Tag, url: str) -> int | None:
    if li.get("data-id", "").isdigit():
        return int(li["data-id"])
    m = re.match(r"module-(\d+)$", li.get("id", ""))
    if m:
        return int(m.group(1))
    return _query_int(url, "id")


def _parse_activity(li: Tag, course_id: int, section: str, base_url: str) -> dict | None:
    link = li.select_one(".activityname a[href], a.aalink[href], .activityinstance a[href]")
    if link is None:
        return None
    url = urljoin(base_url, link["href"])
    activity_id = _activity_id(li, url)
    if activity_id is None:
        return None
    name = text_of(li.select_one(".instancename") or link)
    return {
        "id": activity_id,
        "course_id": course_id,
        "section": section,
        "kind": _activity_kind(li),
        "name": name,
        "url": url,
    }


def parse_course_page(html: str, course_id: int, base_url: str) -> tuple[list[dict], list[str]]:
    """Activities on a course page, plus links to further section pages.

    Courses set to 'one section per page' only show section links on the main page;
    the caller fetches those and parses them the same way.
    """
    soup = _soup(html)
    activities: dict[int, dict] = {}

    sections = soup.select("li.section, li[data-for='section']")
    if sections:
        for sec in sections:
            title = sec.select_one(".sectionname, [data-for='section_title']")
            section_name = text_of(title) or sec.get("aria-label", "")
            for li in sec.select("li.activity"):
                act = _parse_activity(li, course_id, section_name, base_url)
                if act:
                    activities.setdefault(act["id"], act)
    else:
        for li in soup.select("li.activity"):
            act = _parse_activity(li, course_id, "", base_url)
            if act:
                activities.setdefault(act["id"], act)

    section_urls = []
    for link in soup.select('a[href*="/course/view.php"][href*="section="]'):
        href = urljoin(base_url, link["href"])
        if _query_int(href, "id") == course_id and href not in section_urls:
            section_urls.append(href)
    # Also Moodle 4.4+ section pages.
    for link in soup.select('a[href*="/course/section.php?id="]'):
        href = urljoin(base_url, link["href"])
        if href not in section_urls:
            section_urls.append(href)
    return list(activities.values()), section_urls


# --- files -----------------------------------------------------------------

def filename_from_url(url: str) -> str:
    return unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or "file"


def parse_file_links(html: str, base_url: str, scope: str | None = None) -> list[dict]:
    """pluginfile.php links (files) inside the page, or inside `scope` if given."""
    soup = _soup(html)
    root = soup.select_one(scope) if scope else soup
    if root is None:
        return []
    files: dict[str, dict] = {}
    for link in root.select('a[href*="pluginfile.php"]'):
        url = urljoin(base_url, link["href"])
        if url not in files:
            files[url] = {"url": url, "name": filename_from_url(url)}
    return list(files.values())


# --- assignments and quizzes -----------------------------------------------

_DATE_LABELS = {
    "opened": "opens", "opens": "opens", "open": "opens", "allow submissions from": "opens",
    "due": "due", "due date": "due",
    "closed": "closes", "closes": "closes", "close": "closes",
    "cut-off date": "cutoff", "remind me to grade by": "grading_due",
    "відкрито": "opens", "відкривається": "opens", "відкрився": "opens",
    "термін здачі": "due", "до": "due", "кінцевий термін": "due",
    "закрито": "closes", "закривається": "closes",
}


def parse_activity_dates(html: str) -> dict:
    """The 'Opened: … / Due: …' block on assignment and quiz pages."""
    dates: dict[str, dict] = {}
    block = _soup(html).select_one("[data-region='activity-dates'], .activity-dates")
    if block is None:
        return dates
    for row in block.find_all("div", recursive=False) or [block]:
        text = text_of(row)
        if ":" not in text:
            continue
        label, value = text.split(":", 1)
        key = _DATE_LABELS.get(label.strip().lower(), label.strip().lower())
        dates[key] = {"text": value.strip(), "iso": parse_moodle_date(value)}
    return dates


def _table_pairs(table: Tag | None) -> dict[str, str]:
    pairs: dict[str, str] = {}
    if table is None:
        return pairs
    for tr in table.select("tr"):
        th, td = tr.find(["th"]), tr.find("td")
        if th is not None and td is not None:
            pairs[text_of(th)] = text_of(td)
    return pairs


def parse_assignment(html: str, base_url: str) -> dict:
    """Assignment page: name, dates, submission status, feedback, description, attachments.

    The description is returned for local storage only; callers must not pass it to an AI
    (some courses forbid putting assignment conditions into prompts).
    """
    soup = _soup(html)
    name_tag = soup.select_one("#region-main h2, [role='main'] h2")
    status = _table_pairs(soup.select_one(".submissionstatustable table"))
    feedback = _table_pairs(soup.select_one(".feedback table"))
    description = soup.select_one(".activity-description, #intro")
    return {
        "name": text_of(name_tag),
        "dates": parse_activity_dates(html),
        "submission_status": status.get("Submission status") or status.get("Статус здачі"),
        "grading_status": status.get("Grading status") or status.get("Статус оцінення"),
        "time_remaining": status.get("Time remaining") or status.get("Залишилось часу"),
        "status_table": status,
        "grade": feedback.get("Grade") or feedback.get("Оцінка"),
        "feedback_comments": feedback.get("Feedback comments") or feedback.get("Відгук"),
        "description": text_of(description),
        "files": parse_file_links(html, base_url, ".activity-description, #intro"),
    }


def parse_quiz(html: str) -> dict:
    soup = _soup(html)
    name_tag = soup.select_one("#region-main h2, [role='main'] h2")
    grade_tag = soup.select_one("#feedback h3, .quizattempt ~ h3")
    return {
        "name": text_of(name_tag),
        "dates": parse_activity_dates(html),
        "grade": text_of(grade_tag) or None,
    }


# --- grades ----------------------------------------------------------------

def parse_grade_report(html: str) -> list[dict]:
    """Grade items from /grade/report/user/index.php?id=COURSE."""
    items = []
    table = _soup(html).select_one("table.user-grade")
    if table is None:
        return items
    for tr in table.select("tr"):
        name_cell = tr.select_one(".column-itemname")
        grade_cell = tr.select_one("td.column-grade")
        if name_cell is None or grade_cell is None:
            continue
        grade = text_of(grade_cell)
        items.append({
            "item": text_of(name_cell),
            "grade": None if grade in ("", "-", "–") else grade,
            "range": text_of(tr.select_one(".column-range")) or None,
            "percentage": text_of(tr.select_one(".column-percentage")) or None,
            "feedback": text_of(tr.select_one(".column-feedback")) or None,
        })
    return items


# --- calendar --------------------------------------------------------------

def parse_upcoming(html: str, base_url: str, default_year: int | None = None) -> list[dict]:
    """Events from /calendar/view.php?view=upcoming."""
    events = []
    for ev in _soup(html).select("[data-event-id]"):
        event_id = ev.get("data-event-id", "")
        if not event_id.isdigit():
            continue
        name = ev.get("data-event-title") or text_of(ev.select_one("h3.name, .name"))
        when_iso, when_text = None, ""
        day_link = ev.select_one('a[href*="view=day"][href*="time="]')
        if day_link is not None:
            when_text = text_of(day_link.parent)
            ts = _query_int(urljoin(base_url, day_link["href"]), "time")
            if ts:
                when_iso = from_day_timestamp(ts, when_text)
        if when_iso is None:
            body = ev.select_one(".description") or ev
            when_text = when_text or text_of(body)
            when_iso = parse_moodle_date(when_text, default_year)
        course_link = ev.select_one('a[href*="/course/view.php"]')
        activity_link = ev.select_one('.card-footer a[href], a.card-link[href]')
        course_id = ev.get("data-course-id")
        events.append({
            "id": int(event_id),
            "name": name,
            "course_id": int(course_id) if course_id and course_id.isdigit() else None,
            "course": text_of(course_link) or None,
            "kind": ev.get("data-event-eventtype"),
            "component": ev.get("data-event-component"),
            "when": when_iso,
            "when_text": when_text,
            "url": urljoin(base_url, activity_link["href"]) if activity_link else None,
        })
    return events


# --- forums ----------------------------------------------------------------

def parse_forum(html: str, base_url: str) -> list[dict]:
    """Discussions listed on a forum page (used for course announcements)."""
    discussions: dict[int, dict] = {}
    for link in _soup(html).select('a[href*="/mod/forum/discuss.php?d="]'):
        url = urljoin(base_url, link["href"])
        d = _query_int(url, "d")
        title = text_of(link)
        if d is None or not title:
            continue
        discussions.setdefault(d, {"id": d, "title": title, "url": url.split("#")[0]})
    return list(discussions.values())
