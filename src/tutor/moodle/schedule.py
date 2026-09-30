"""macOS launchd job: sync when you log in, then every 3 hours while the Mac is awake.

launchd runs a missed interval as soon as the Mac wakes, so a closed lid only delays it.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import Config

LABEL = "ua.kse.tutor.moodle-sync"
INTERVAL_SECONDS = 3 * 60 * 60


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(cfg: Config, tutor_bin: str) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [tutor_bin, "moodle-sync"],
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(cfg.log_path),
        "StandardErrorPath": str(cfg.log_path),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    }


def _tutor_bin() -> str:
    found = shutil.which("tutor")
    if found:
        return found
    candidate = Path(sys.executable).with_name("tutor")
    if candidate.exists():
        return str(candidate)
    raise RuntimeError("Can't find the `tutor` command. Install with `uv tool install .` first.")


def install(cfg: Config) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("The schedule uses launchd and only works on macOS.")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    uninstall(quiet=True)
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(cfg, _tutor_bin()), fh)
    for attempt in range(3):  # right after bootout, launchd can refuse briefly
        done = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
                              capture_output=True, text=True)
        if done.returncode == 0:
            return path
        time.sleep(2)
    raise RuntimeError(f"launchctl could not load the schedule: {done.stderr.strip()}")


def is_installed() -> bool:
    if sys.platform != "darwin" or not plist_path().exists():
        return False
    done = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    return done.returncode == 0


def uninstall(quiet: bool = False) -> None:
    path = plist_path()
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       check=False, capture_output=quiet)
    if path.exists():
        path.unlink()
