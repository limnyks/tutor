from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://teaching.kse.org.ua"


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    home: Path = field(default_factory=lambda: Path.home() / ".tutor")
    # Where downloaded course files go. Point this at a Google Drive folder to sync them.
    files_dir: Path = field(default_factory=lambda: Path.home() / "Tutor" / "Moodle")
    # tutor-state repo checkout. When set, change events are also appended there.
    state_dir: Path | None = None
    # KSE Google account, used only to pick the right account on Google's chooser page.
    google_account: str | None = None
    # Force Moodle's English interface so dates parse the same way every time.
    lang: str | None = "en"
    request_delay: float = 1.5
    max_file_mb: int = 200
    # Course ids or name fragments to skip entirely.
    courses_ignore: list[str] = field(default_factory=list)

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    @property
    def profile_dir(self) -> Path:
        return self.home / "moodle-profile"

    @property
    def data_dir(self) -> Path:
        return self.home / "moodle"

    @property
    def snapshot_path(self) -> Path:
        return self.data_dir / "snapshot.json"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "files.json"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def events_path(self) -> Path:
        return self.data_dir / "events.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "sync.lock"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "sync.log"

    def url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        return self.base_url.rstrip("/") + "/" + path_or_url.lstrip("/")


_PATH_FIELDS = {"home", "files_dir", "state_dir"}


def config_path() -> Path:
    env = os.environ.get("TUTOR_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".tutor" / "config.json"


def load_config(path: Path | None = None) -> Config:
    """Defaults, overridden by ~/.tutor/config.json (or $TUTOR_CONFIG)."""
    path = path or config_path()
    cfg = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        for key, value in raw.items():
            if not hasattr(cfg, key):
                raise ValueError(f"Unknown config key in {path}: {key}")
            if key in _PATH_FIELDS and value is not None:
                value = Path(value).expanduser()
            setattr(cfg, key, value)
    return cfg
