"""YouTube login cookies for the command-line tools (members-only streams and chat).

Cookies come from a browser profile (Firefox works everywhere; Chromium browsers on Windows
lock their cookie database with app-bound encryption) or from a Netscape cookies.txt file.
"""

from __future__ import annotations

import http.cookiejar
import logging
import time
from dataclasses import dataclass

from .config import AccountConfig

log = logging.getLogger(__name__)

BROWSERS = ["firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium", "safari"]


@dataclass
class CookieSource:
    browser: str = ""
    file: str = ""

    @classmethod
    def from_config(cls, account: AccountConfig) -> CookieSource | None:
        if account.cookies_file:
            return cls(file=account.cookies_file)
        if account.cookies_browser:
            return cls(browser=account.cookies_browser)
        return None

    def ytdlp_params(self) -> dict:
        if self.file:
            return {"cookiefile": self.file}
        if self.browser:
            return {"cookiesfrombrowser": (self.browser, None, None, None)}
        return {}

    def describe(self) -> str:
        return f"cookies.txt ({self.file})" if self.file else f"{self.browser} browser"


_CACHE: dict[tuple[str, str], tuple[float, http.cookiejar.CookieJar]] = {}


def load_jar(source: CookieSource | None, max_age: float = 300.0) -> http.cookiejar.CookieJar | None:
    """Load cookies (cached for a few minutes; browser extraction is slow)."""
    if source is None:
        return None
    key = (source.browser, source.file)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import load_cookies

    with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        jar = load_cookies(
            source.file or None,
            (source.browser,) if source.browser and not source.file else None,
            ydl,
        )
    _CACHE[key] = (time.monotonic(), jar)
    return jar


def youtube_cookies(jar: http.cookiejar.CookieJar | None) -> dict[str, str]:
    if jar is None:
        return {}
    out: dict[str, str] = {}
    for cookie in jar:
        domain = cookie.domain.lstrip(".")
        if domain.endswith("youtube.com") and cookie.value is not None:
            # Prefer exact youtube.com cookies over subdomain duplicates.
            if cookie.name not in out or domain == "youtube.com":
                out[cookie.name] = cookie.value
    return out
