"""Local state: snapshot, file manifest, status, event log, and the sync lock."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import Config

SOURCE = "mac-moodle"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, data) -> None:
    """Atomic write, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, path)


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def sync_lock(cfg: Config) -> Iterator[None]:
    """One sync at a time, whether started by launchd, the CLI or the MCP server."""
    cfg.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("Another Moodle sync is running.") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def make_event(type_: str, key: str, data: dict, course: dict | None = None) -> dict:
    """An event record. The id is stable for the same fact, so re-logging is harmless."""
    fingerprint = json.dumps([type_, key, data], ensure_ascii=False, sort_keys=True)
    return {
        "id": hashlib.sha1(fingerprint.encode()).hexdigest()[:16],
        "ts": now_iso(),
        "source": SOURCE,
        "type": type_,
        "course_id": course["id"] if course else None,
        "course": course["name"] if course else None,
        "data": data,
    }


def append_events(cfg: Config, events: list[dict]) -> None:
    if not events:
        return
    targets = [cfg.events_path]
    if cfg.state_dir:
        day = datetime.now().strftime("%Y-%m-%d")
        targets.append(cfg.state_dir / "events" / f"{day}.{SOURCE}.jsonl")
    lines = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(lines)


def read_events(cfg: Config) -> list[dict]:
    if not cfg.events_path.exists():
        return []
    with open(cfg.events_path) as fh:
        return [json.loads(line) for line in fh if line.strip()]
