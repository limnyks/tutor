"""The logged-in Chrome session used to read Moodle.

Login happens once, by hand, in a normal Chrome window (see `open_login_window`).
Afterwards the same profile runs headless. When Moodle's own session expires, the
reader follows 'Log in with Google' again; that completes by itself while the Google
session is still valid. When Google itself wants a password or 2FA, it stops and
reports LoginRequired.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import Config
from .files import Fetched
from .parse import filename_from_url, is_login_page, oauth_login_url

# Hosts the headless reader may navigate to. Anything else is blocked.
GOOGLE_LOGIN_HOSTS = ("accounts.google.com", "accounts.youtube.com")

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class LoginRequired(RuntimeError):
    pass


def chrome_path() -> str | None:
    """Real Chrome. $TUTOR_CHROME overrides (any Chromium binary)."""
    if os.environ.get("TUTOR_CHROME"):
        return os.environ["TUTOR_CHROME"]
    if sys.platform == "darwin" and Path(MAC_CHROME).exists():
        return MAC_CHROME
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_login_window(cfg: Config) -> None:
    """Open plain Chrome (not automated) on the Moodle login page and wait until it closes.

    Plain Chrome, because Google refuses sign-in from browsers under automation.
    """
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    chrome = chrome_path()
    login_url = cfg.url("/login/index.php")
    if chrome:
        subprocess.run([
            chrome,
            f"--user-data-dir={cfg.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            login_url,
        ], check=False)
        return
    # Fallback without Chrome installed: a Playwright window.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(cfg.profile_dir), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(login_url)
        page.wait_for_event("close", timeout=0)
        ctx.close()


def _filename(headers: dict, url: str) -> str:
    disposition = headers.get("content-disposition", "")
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        name = msg.get_filename()
        if name:
            return unquote(name)
    return filename_from_url(url)


class MoodleSession:
    """Context manager around a headless Chrome using the saved profile."""

    def __init__(self, cfg: Config, headless: bool = True):
        self.cfg = cfg
        self.headless = headless
        self._reauth_tried = False

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "MoodleSession":
        from playwright.sync_api import sync_playwright

        if not self.cfg.profile_dir.exists():
            raise LoginRequired("No saved login yet. Run: tutor moodle-login")
        self._pw = sync_playwright().start()
        kwargs = dict(
            headless=self.headless,
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        if os.environ.get("TUTOR_CHROME"):
            kwargs["executable_path"] = os.environ["TUTOR_CHROME"]
        elif chrome_path():
            kwargs["channel"] = "chrome"
        self.ctx = self._pw.chromium.launch_persistent_context(str(self.cfg.profile_dir), **kwargs)
        self.ctx.route("**/*", self._guard)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if self.cfg.lang:
            self.get_html(f"/?lang={self.cfg.lang}")
        return self

    def __exit__(self, *exc) -> None:
        self.ctx.close()
        self._pw.stop()

    # -- safety -------------------------------------------------------------

    def _guard(self, route, request) -> None:
        """Read-only and Moodle-only.

        Navigation is limited to Moodle and Google's sign-in pages. Requests to Moodle
        other than GET are blocked, except the AJAX endpoint Moodle's own pages use to
        render (course lists, calendar).
        """
        host = urlparse(request.url).hostname or ""
        if request.is_navigation_request() and host != self.cfg.host and host not in GOOGLE_LOGIN_HOSTS:
            route.abort()
            return
        if host == self.cfg.host and request.method not in ("GET", "HEAD"):
            if "/lib/ajax/service.php" not in request.url:
                route.abort()
                return
        route.continue_()

    # -- reading ------------------------------------------------------------

    def _pause(self) -> None:
        time.sleep(self.cfg.request_delay)

    def get_html(self, path_or_url: str) -> tuple[str, str]:
        """Open a page, wait for it to render, return (final_url, html)."""
        self._pause()
        url = self.cfg.url(path_or_url)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            if "chrome-error" not in str(exc) and "interrupted" not in str(exc):
                raise
            # A blocked navigation leaves the tab on an error page; start from a fresh tab.
            self.page.close()
            self.page = self.ctx.new_page()
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass  # some pages keep polling; the DOM is ready anyway
        url, html = self.page.url, self.page.content()
        if is_login_page(url, html):
            self._reauthenticate(url, html)
            return self.get_html(path_or_url)
        return url, html

    def _reauthenticate(self, url: str, html: str) -> None:
        """Follow 'Log in with Google' once. Succeeds only if no password/2FA is asked."""
        if self._reauth_tried:
            raise LoginRequired("Moodle login expired and Google needs you to sign in again.")
        self._reauth_tried = True
        oauth = oauth_login_url(html, self.cfg.base_url) if self.cfg.host in url else None
        if oauth:
            self.page.goto(oauth, wait_until="domcontentloaded", timeout=60_000)
        if self.cfg.google_account and "accounts.google.com" in self.page.url:
            chooser = self.page.locator(f'[data-identifier="{self.cfg.google_account}"]')
            if chooser.count():
                chooser.first.click()
        try:
            self.page.wait_for_url(
                lambda u: urlparse(u).hostname == self.cfg.host and "/login/" not in u,
                timeout=30_000,
            )
        except Exception as exc:
            raise LoginRequired("Google needs you to sign in again (password or 2FA).") from exc

    def head_final_url(self, url: str) -> str | None:
        self._pause()
        try:
            resp = self.ctx.request.head(self.cfg.url(url), timeout=60_000)
        except Exception:
            return None
        return resp.url if resp.ok else None

    def fetch(self, url: str) -> Fetched:
        """Download a file with the session's cookies."""
        self._pause()
        resp = self.ctx.request.get(self.cfg.url(url), timeout=180_000)
        if urlparse(resp.url).hostname in GOOGLE_LOGIN_HOSTS or "/login/" in resp.url:
            raise LoginRequired("Moodle login expired.")
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return Fetched(
            body=resp.body(),
            filename=_filename(headers, resp.url),
            content_type=headers.get("content-type", ""),
            final_url=resp.url,
        )


def notify(title: str, message: str) -> None:
    """A macOS notification (no-op elsewhere)."""
    if sys.platform != "darwin":
        return
    subprocess.run([
        "osascript",
        "-e", "on run argv",
        "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e", "end run",
        title, message,
    ], check=False)
