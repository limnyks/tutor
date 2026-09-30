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


def fix_mojibake(name: str) -> str:
    """Undo UTF-8 bytes read as Latin-1 ('ÐÑÐ°Ð…' -> 'Практична…').

    Header values arrive decoded as Latin-1, so a Cyrillic filename sent as raw UTF-8
    comes out garbled. Plain ASCII and correctly decoded names are left unchanged.
    """
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def repair_names(manifest: dict) -> int:
    """Rename files saved earlier under a garbled (Latin-1-decoded) name. Returns how many."""
    fixed = 0
    for key, entry in list(manifest.items()):
        path = Path(entry["path"]) if entry.get("path") else None
        if path is None:
            continue
        good = fix_mojibake(path.name)
        if good == path.name:
            continue
        new_path = path.with_name(safe_name(good))
        if path.exists() and not new_path.exists():
            path.rename(new_path)
        activity_id, _, name = key.partition(":")
        del manifest[key]
        manifest[f"{activity_id}:{fix_mojibake(name)}"] = {**entry, "path": str(new_path)}
        fixed += 1
    return fixed


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
    _rename_if_same_content(cfg, manifest, key, url, digest, course, section, got.filename, activity)
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


def _rename_if_same_content(cfg, manifest, key, url, digest, course, section, filename, activity) -> None:
    """Same file, new name (renamed on Moodle, or a name we used to decode wrongly): move it."""
    if key in manifest:
        return
    for old_key, old in list(manifest.items()):
        if old.get("source_url") == url and old.get("sha256") == digest and old.get("path"):
            old_path = Path(old["path"])
            del manifest[old_key]
            folder = cfg.files_dir / safe_name(course["name"]) / safe_name(section or "General")
            new_path = _free_path(manifest, folder, filename, activity)
            if old_path.exists():
                new_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.rename(new_path)
            manifest[key] = {**old, "path": str(new_path)}
            return


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
