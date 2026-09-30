"""Turn two snapshots into change events. Only what changed becomes an event."""

from __future__ import annotations

from .store import make_event

_TRACKED_ASSIGNMENT_FIELDS = ("submission_status", "grading_status", "grade", "feedback_comments")


def _due(item: dict) -> str | None:
    dates = item.get("dates") or {}
    for key in ("due", "closes", "cutoff"):
        if dates.get(key):
            return dates[key].get("iso") or dates[key].get("text")
    return None


def diff_snapshots(old: dict | None, new: dict) -> list[dict]:
    courses = new.get("courses", {})

    def course_of(course_id) -> dict | None:
        return courses.get(str(course_id))

    if not old:
        return [make_event("baseline", "first-sync", {
            "courses": len(courses),
            "activities": len(new.get("activities", {})),
            "deadlines": len(new.get("deadlines", {})),
        })]

    events: list[dict] = []

    for cid, course in courses.items():
        if cid not in old.get("courses", {}):
            events.append(make_event("new_course", cid, {"name": course["name"]}, course))

    old_courses = old.get("courses", {})
    old_acts = old.get("activities", {})
    for aid, act in new.get("activities", {}).items():
        if str(act["course_id"]) not in old_courses:
            continue  # a whole new course: its new_course event covers its contents
        if aid not in old_acts:
            events.append(make_event("new_activity", aid, {
                "kind": act["kind"], "name": act["name"], "section": act["section"], "url": act["url"],
            }, course_of(act["course_id"])))

    old_items = {**old.get("assignments", {}), **old.get("quizzes", {})}
    new_items = {**new.get("assignments", {}), **new.get("quizzes", {})}
    for aid, item in new_items.items():
        prev = old_items.get(aid)
        course = course_of(item.get("course_id"))
        if prev is None:
            continue  # announced by new_activity
        if _due(prev) != _due(item) and _due(item):
            events.append(make_event("due_date_changed", aid, {
                "name": item["name"], "old": _due(prev), "new": _due(item),
            }, course))
        for field in _TRACKED_ASSIGNMENT_FIELDS:
            if item.get(field) != prev.get(field) and item.get(field):
                events.append(make_event(f"{field}_changed", aid, {
                    "name": item["name"], "old": prev.get(field), "new": item.get(field),
                }, course))

    old_grades = old.get("grades", {})
    for cid, items in new.get("grades", {}).items():
        if cid not in old_grades:
            continue  # first read of this course's grades: nothing to compare with
        before = old_grades.get(cid, {})
        for name, g in items.items():
            prev = before.get(name, {})
            if g.get("grade") and g.get("grade") != prev.get("grade"):
                events.append(make_event("new_grade" if not prev.get("grade") else "grade_changed", f"{cid}:{name}", {
                    "item": name, "grade": g["grade"], "old": prev.get("grade"),
                    "range": g.get("range"), "percentage": g.get("percentage"),
                }, course_of(cid)))
            if g.get("feedback") and g.get("feedback") != prev.get("feedback"):
                events.append(make_event("new_feedback", f"{cid}:{name}", {
                    "item": name, "feedback": g["feedback"],
                }, course_of(cid)))

    old_deadlines = old.get("deadlines", {})
    for eid, ev in new.get("deadlines", {}).items():
        prev = old_deadlines.get(eid)
        course = course_of(ev.get("course_id"))
        if prev is None:
            events.append(make_event("new_deadline", eid, {
                "name": ev["name"], "when": ev.get("when"), "url": ev.get("url"),
            }, course))
        elif prev.get("when") != ev.get("when"):
            events.append(make_event("deadline_changed", eid, {
                "name": ev["name"], "old": prev.get("when"), "new": ev.get("when"),
            }, course))

    old_ann = old.get("announcements", {})
    for cid, posts in new.get("announcements", {}).items():
        if cid not in old_ann:
            continue  # first read of this forum: existing posts aren't news
        for did, post in posts.items():
            if did not in old_ann.get(cid, {}):
                events.append(make_event("new_announcement", f"{cid}:{did}", {
                    "title": post["title"], "url": post["url"],
                }, course_of(cid)))

    return events
