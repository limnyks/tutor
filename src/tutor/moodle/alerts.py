"""macOS notifications after a sync: deadlines coming up, grades, feedback, new tasks.

Each alert is sent once (remembered in alerts.json). Alerts that come up during quiet
hours wait for the first sync after them. New files are not alerted: the tutor's
briefing covers those.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .browser import notify
from .config import Config
from .queries import NOT_SUBMITTED, points_lost, upcoming_deadlines
from .store import read_json, write_json

MAX_SEPARATE = 3
DEADLINE_THRESHOLDS = [(24, "24h"), (48, "48h")]  # smallest first


def _code(course_name: str | None) -> str:
    """'STAT2100 Probability …' -> 'STAT2100'; other names shortened."""
    if not course_name:
        return "Moodle"
    first = course_name.split()[0]
    return first if any(ch.isdigit() for ch in first) else course_name[:24]


def _event_alert(ev: dict) -> tuple[str, str] | None:
    d, code = ev.get("data", {}), _code(ev.get("course"))
    kind = ev.get("type")
    if kind in ("new_grade", "grade_changed"):
        # Never points: only whether something was lost, and where to get the feedback.
        lost = points_lost(d)
        verdict = {True: "points lost — not enough for your target; ask the tutor what to fix",
                   False: "nothing lost",
                   None: "graded — ask the tutor for feedback"}[lost]
        return f"grade:{ev['id']}", f"{code}: {d.get('item')}: {verdict}"
    if kind == "new_feedback":
        return f"feedback:{ev['id']}", f"{code}: teacher feedback on {d.get('item')}"
    if kind == "new_activity" and d.get("kind") in ("assign", "quiz"):
        what = "assignment" if d["kind"] == "assign" else "quiz"
        return f"new:{ev['id']}", f"{code}: new {what}: {d.get('name')}"
    if kind in ("due_date_changed", "deadline_changed"):
        return f"moved:{ev['id']}", f"{code}: deadline moved: {d.get('name')}"
    if kind == "new_announcement":
        return f"ann:{ev['id']}", f"{code}: announcement: {d.get('title')}"
    return None


def _deadline_alerts(snap: dict, now: datetime, sent: dict) -> list[tuple[str, str]]:
    out = []
    for item in upcoming_deadlines(snap, now, days=2, overdue_days=0):
        status = item.get("submission_status")
        if item["kind"] == "assignment" and status and not NOT_SUBMITTED.search(status):
            continue  # already submitted
        hours = (datetime.fromisoformat(item["when"]) - now).total_seconds() / 3600
        base = f"due:{item.get('url') or item['name']}"
        for limit, tag in DEADLINE_THRESHOLDS:
            if hours <= limit:
                if not any(f"{base}:{t}" in sent for lim, t in DEADLINE_THRESHOLDS if lim <= limit):
                    when = datetime.fromisoformat(item["when"]).astimezone().strftime("%a %H:%M")
                    out.append((f"{base}:{tag}",
                                f"{_code(item.get('course'))}: {item['name']} — due {when} (in {hours:.0f} h)"))
                break
    return out


def in_quiet_hours(now: datetime, quiet: list[int] | None) -> bool:
    if not quiet:
        return False
    start, end = quiet
    hour = now.astimezone().hour
    return start <= hour or hour < end if start > end else start <= hour < end


def send_alerts(cfg: Config, events: list[dict], snap: dict, now: datetime, send=notify) -> list[str]:
    """Work out and deliver this sync's alerts. Returns the messages delivered."""
    state = read_json(cfg.data_dir / "alerts.json", {"sent": {}, "pending": []})
    sent, pending = state["sent"], state["pending"]

    fresh = [a for a in (_event_alert(e) for e in events) if a] + _deadline_alerts(snap, now, sent)
    queued_keys = {key for key, _ in pending}
    for key, msg in fresh:
        if key not in sent and key not in queued_keys:
            pending.append([key, msg])
            queued_keys.add(key)

    delivered = []
    if pending and not in_quiet_hours(now, cfg.notify_quiet_hours):
        batch = pending if len(pending) <= MAX_SEPARATE else pending[:MAX_SEPARATE - 1]
        for key, msg in batch:
            send("Tutor", msg)
            delivered.append(msg)
        if len(pending) > MAX_SEPARATE:
            rest = len(pending) - len(batch)
            send("Tutor", f"{rest} more Moodle changes — ask the tutor what changed")
            delivered.append(f"{rest} more")
        for key, _ in pending:
            sent[key] = now.isoformat()
        pending = []

    # Forget sent keys older than 60 days, so the file stays small.
    cutoff = (now - timedelta(days=60)).isoformat()
    state = {"sent": {k: v for k, v in sent.items() if v >= cutoff}, "pending": pending}
    write_json(cfg.data_dir / "alerts.json", state)
    return delivered
