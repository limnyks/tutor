"""The event log: one JSON line per fact, one file per day and writer, never edited."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

# Event types written by the tutor (Moodle's own events come from the connector).
MEMORY_TYPES = {"answer", "study", "summary"}


def writer_name() -> str:
    """Separate files per machine, so git never has to merge two writers' lines."""
    host = socket.gethostname().split(".")[0].lower() or "host"
    return f"tutor-{host}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(memory_dir: Path, type_: str, data: dict, course: str | None = None) -> dict:
    if type_ not in MEMORY_TYPES:
        raise ValueError(f"Unknown event type: {type_}")
    ts = now_iso()
    event = {
        "id": hashlib.sha1(json.dumps([ts, type_, course, data], sort_keys=True, ensure_ascii=False)
                           .encode()).hexdigest()[:16],
        "ts": ts,
        "source": writer_name(),
        "type": type_,
        "course": course,
        "data": data,
    }
    day = datetime.now().strftime("%Y-%m-%d")
    path = memory_dir / "events" / f"{day}.{writer_name()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def read_all(memory_dir: Path) -> list[dict]:
    """Every event from every writer, oldest first. Unreadable lines are skipped, not fatal."""
    events = []
    for path in sorted((memory_dir / "events").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    events.sort(key=lambda e: e.get("ts", ""))
    return events
