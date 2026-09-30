"""Keep the memory repo in step with GitHub: commit new events, pull others', push ours.

Writers only ever add their own per-day files, so pulls never conflict. Failures (no
network, no credentials) are reported, never fatal: memory keeps working locally and
catches up at the next sync.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

GIT_IDENTITY = ["-c", "user.name=tutor", "-c", "user.email=tutor@localhost"]


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    # Never wait for a password prompt (background runs have no terminal).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(repo), *GIT_IDENTITY, *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def publish_snapshot(snapshot: dict, memory_dir: Path) -> None:
    """Copy the Moodle snapshot into the memory repo for the cloud and phone, without
    assignment conditions (never to be put into an AI prompt)."""
    clean = json.loads(json.dumps(snapshot))
    for item in clean.get("assignments", {}).values():
        item.pop("description", None)
    path = memory_dir / "moodle" / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(path)


def sync(memory_dir: Path, message: str = "tutor: update memory") -> str:
    """Commit local changes, pull, push. Returns a one-line outcome."""
    if not (memory_dir / ".git").exists():
        return f"not a git repository: {memory_dir}"
    try:
        _git(memory_dir, "add", "-A")
        staged = _git(memory_dir, "diff", "--cached", "--quiet").returncode != 0
        if staged:
            done = _git(memory_dir, "commit", "-q", "-m", message)
            if done.returncode != 0:
                return f"commit failed: {done.stderr.strip()[:200]}"
        if not _git(memory_dir, "remote").stdout.strip():
            return "committed locally (no remote)" if staged else "nothing to sync (no remote)"
        has_upstream = _git(memory_dir, "rev-parse", "--abbrev-ref", "@{u}").returncode == 0
        if has_upstream:
            pulled = _git(memory_dir, "pull", "-q", "--rebase", "--autostash")
            if pulled.returncode != 0:
                _git(memory_dir, "rebase", "--abort")
                return f"pull failed, kept local copy: {pulled.stderr.strip()[:200]}"
        pushed = _git(memory_dir, "push", "-q", "-u", "origin", "HEAD")
        if pushed.returncode != 0:
            return f"push failed, kept local copy: {pushed.stderr.strip()[:200]}"
        return "synced with GitHub"
    except subprocess.TimeoutExpired:
        return "git timed out, kept local copy"
