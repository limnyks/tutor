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


# fetch(url) -> Fetched ; head(url) -> (final url after redirects, size), None when unknown
Fetch = Callable[[str], Fetched]
Head = Callable[[str], "tuple[str | None, int | None]"]

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
    limit = cfg.max_file_mb * 1024 * 1024
    known = [k for k, v in manifest.items() if v.get("source_url") == url]
    if head is not None:
        final_url, size = head(url)
        if known and final_url and final_url == manifest[known[0]].get("final_url"):
            return None
        if size and size > limit:
            return _skip_too_large(manifest, course, activity, url, final_url, size)

    got = fetch(url)
    if len(got.body) > limit:
        return _skip_too_large(manifest, course, activity, url, got.final_url, len(got.body))
    digest = hashlib.sha256(got.body).hexdigest()
    key = file_key(activity["id"], got.filename)
    entry = manifest.get(key)
    if entry and entry.get("sha256") == digest:
        entry["final_url"] = got.final_url
        return None

    if entry and entry.get("path"):
        path = Path(entry["path"])
    else:
        folder = cfg.files_dir / safe_name(course["name"]) / safe_name(section or "General")
        path = _free_path(manifest, folder, got.filename, activity)
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


def _free_path(manifest: dict, folder: Path, filename: str, activity: dict) -> Path:
    """A path no other activity's file uses. Teachers often reuse names like 'Lecture.pdf'."""
    taken = {v.get("path") for v in manifest.values()}
    path = folder / safe_name(filename)
    if str(path) not in taken and not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for tag in (activity["name"], str(activity["id"])):
        candidate = folder / safe_name(f"{stem} ({tag}){suffix}")
        if str(candidate) not in taken and not candidate.exists():
            return candidate
    return folder / safe_name(f"{stem} ({activity['id']}-{len(taken)}){suffix}")


def _skip_too_large(manifest, course, activity, url, final_url, size) -> dict | None:
    """Remember an oversized file (e.g. a lecture video) so it is reported once, not fetched."""
    key = f"too-large:{url}"
    if key in manifest and manifest[key].get("final_url") == final_url:
        return None
    manifest[key] = {"source_url": url, "final_url": final_url, "size": size, "skipped": True,
                     "course_id": course["id"], "activity_id": activity["id"], "activity": activity["name"],
                     "path": "", "section": activity.get("section", ""), "graded": False,
                     "downloaded_at": None}
    return make_event("file_too_large", key, {"activity": activity["name"], "size_mb": round(size / 2**20),
                                              "url": url}, course)


def _keep_old_version(path: Path) -> None:
    versions = path.parent / ".versions"
    versions.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path.rename(versions / f"{path.stem}.{stamp}{path.suffix}")
