"""Downloading course files into files_dir, keeping old versions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config
from .store import make_event, now_iso


@dataclass
class Fetched:
    body: bytes
    filename: str
    content_type: str
    final_url: str


# fetch(url) -> Fetched ; head(url) -> final url after redirects (None if unknown)
Fetch = Callable[[str], Fetched]
Head = Callable[[str], "str | None"]

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str, limit: int = 120) -> str:
    cleaned = _UNSAFE.sub("_", name).strip(" .") or "untitled"
    return cleaned[:limit]


def file_key(activity_id: int, filename: str) -> str:
    return f"{activity_id}:{filename}"


def save_file(
    cfg: Config,
    manifest: dict,
    *,
    course: dict,
    section: str,
    activity: dict,
    url: str,
    fetch: Fetch,
    head: Head | None = None,
) -> dict | None:
    """Download one file if it is new or changed. Returns a change event, or None.

    Unchanged files are detected by their final URL (Moodle puts a revision number in
    it) when `head` is given, and otherwise by content hash.
    """
    graded = activity["kind"] in ("assign", "quiz")
    known = [k for k, v in manifest.items() if v.get("source_url") == url]
    if head is not None and known:
        final_url = head(url)
        if final_url and final_url == manifest[known[0]].get("final_url"):
            return None

    got = fetch(url)
    if len(got.body) > cfg.max_file_mb * 1024 * 1024:
        return None
    digest = hashlib.sha256(got.body).hexdigest()
    key = file_key(activity["id"], got.filename)
    entry = manifest.get(key)
    if entry and entry["sha256"] == digest:
        entry["final_url"] = got.final_url
        return None

    folder = cfg.files_dir / safe_name(course["name"]) / safe_name(section or "General")
    path = folder / safe_name(got.filename)
    if path.exists() and entry:
        _keep_old_version(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(got.body)

    manifest[key] = {
        "path": str(path),
        "sha256": digest,
        "size": len(got.body),
        "content_type": got.content_type,
        "source_url": url,
        "final_url": got.final_url,
        "course_id": course["id"],
        "activity_id": activity["id"],
        "activity": activity["name"],
        "section": section,
        # Files attached to graded work: stored, but never to be put into an AI prompt.
        "graded": graded,
        "downloaded_at": now_iso(),
    }
    return make_event("file_updated" if entry else "new_file", key, {
        "name": got.filename, "path": str(path), "activity": activity["name"],
        "section": section, "graded": graded, "sha256": digest,
    }, course)


def _keep_old_version(path: Path) -> None:
    versions = path.parent / ".versions"
    versions.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path.rename(versions / f"{path.stem}.{stamp}{path.suffix}")
