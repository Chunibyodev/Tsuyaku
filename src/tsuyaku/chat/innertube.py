"""Minimal helpers for YouTube's internal web API (the one youtube.com itself uses)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

ORIGIN = "https://www.youtube.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_INITIAL_DATA = re.compile(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*')
_YTCFG = re.compile(r"ytcfg\.set\s*\(\s*(\{.+?\})\s*\)\s*;", re.S)


class ChatUnavailable(Exception):
    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


def extract_initial_data(html: str) -> dict:
    m = _INITIAL_DATA.search(html)
    if not m:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(html[m.end():])
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def extract_ytcfg(html: str) -> dict:
    cfg: dict = {}
    for m in _YTCFG.finditer(html):
        try:
            cfg.update(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return cfg


def dig(obj: Any, *path: Any, default: Any = None) -> Any:
    for key in path:
        try:
            obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return default
    return obj


def sid_authorization(cookies: dict[str, str], user_session_id: str | None = None,
                      origin: str = ORIGIN, now: float | None = None) -> str | None:
    """SAPISIDHASH header for cookie-authenticated requests (same scheme as youtube.com)."""
    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
    one_p = cookies.get("__Secure-1PAPISID")
    three_p = cookies.get("__Secure-3PAPISID")
    ts = str(round(now if now is not None else time.time()))
    parts = []
    for scheme, sid in (("SAPISIDHASH", sapisid), ("SAPISID1PHASH", one_p), ("SAPISID3PHASH", three_p)):
        if not sid:
            continue
        hash_parts = []
        if user_session_id:
            hash_parts.append(user_session_id)
        hash_parts += [ts, sid, origin]
        digest = hashlib.sha1(" ".join(hash_parts).encode()).hexdigest()
        value = f"{ts}_{digest}" + ("_u" if user_session_id else "")
        parts.append(f"{scheme} {value}")
    return " ".join(parts) if parts else None


def parse_data_sync_id(value: str | None) -> tuple[str | None, str | None]:
    """'delegated||user' (brand account) or 'user||' -> (delegated_session_id, user_session_id)."""
    if not value:
        return None, None
    first, _, second = value.partition("||")
    if second:
        return first, second
    return None, first


@dataclass
class ChatPage:
    video_id: str
    ytcfg: dict
    initial: dict
    logged_in: bool = False
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def renderer(self) -> dict:
        return dig(self.initial, "contents", "liveChatRenderer", default={}) or {}

    @property
    def context(self) -> dict:
        ctx = dict(self.ytcfg.get("INNERTUBE_CONTEXT") or {})
        if not ctx:
            ctx = {"client": {"clientName": "WEB", "clientVersion": self.client_version, "hl": "en", "gl": "US"}}
        return ctx

    @property
    def client_version(self) -> str:
        return self.ytcfg.get("INNERTUBE_CLIENT_VERSION") or "2.20250101.00.00"

    @property
    def api_key(self) -> str:
        return self.ytcfg.get("INNERTUBE_API_KEY") or ""

    def continuation(self, mode: str = "live") -> str | None:
        r = self.renderer
        if mode == "live":
            items = dig(r, "header", "liveChatHeaderRenderer", "viewSelector",
                        "sortFilterSubMenuRenderer", "subMenuItems", default=[]) or []
            for item in items:
                title = (item.get("title") or "").lower()
                cont = dig(item, "continuation", "reloadContinuationData", "continuation")
                if cont and ("live" in title and "top" not in title):
                    return cont
            if len(items) > 1:
                cont = dig(items[1], "continuation", "reloadContinuationData", "continuation")
                if cont:
                    return cont
        for c in r.get("continuations", []) or []:
            for key in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
                if key in c and c[key].get("continuation"):
                    return c[key]["continuation"]
        return None

    def headers(self) -> dict[str, str]:
        h = {
            "User-Agent": USER_AGENT,
            "Origin": ORIGIN,
            "X-Youtube-Client-Name": str(self.ytcfg.get("INNERTUBE_CONTEXT_CLIENT_NAME") or "1"),
            "X-Youtube-Client-Version": self.client_version,
        }
        visitor = self.ytcfg.get("VISITOR_DATA") or dig(self.ytcfg, "INNERTUBE_CONTEXT", "client", "visitorData")
        if visitor:
            h["X-Goog-Visitor-Id"] = visitor
        if self.cookies:
            delegated, user_sid = parse_data_sync_id(self.ytcfg.get("DATASYNC_ID"))
            delegated = self.ytcfg.get("DELEGATED_SESSION_ID") or delegated
            user_sid = self.ytcfg.get("USER_SESSION_ID") or user_sid
            auth = sid_authorization(self.cookies, user_sid if self.logged_in else None)
            if auth and self.logged_in:
                h["Authorization"] = auth
                h["X-Origin"] = ORIGIN
                h["X-Goog-AuthUser"] = str(self.ytcfg.get("SESSION_INDEX") or 0)
                if delegated:
                    h["X-Goog-PageId"] = delegated
                h["X-Youtube-Bootstrap-Logged-In"] = "true"
        return h


def _plain(obj: Any) -> str:
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    return "".join(r.get("text", "") for r in obj.get("runs", []))


async def fetch_chat_page(client: httpx.AsyncClient, video_id: str, cookies: dict[str, str] | None = None) -> ChatPage:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    jar = dict(cookies or {})
    jar.setdefault("SOCS", "CAI")  # skip the EU consent interstitial
    resp = await client.get(
        "https://www.youtube.com/live_chat",
        params={"is_popout": "1", "v": video_id},
        headers=headers,
        cookies=jar,
    )
    if resp.status_code == 429:
        raise ChatUnavailable("YouTube is rate limiting chat requests.", "rate_limited")
    resp.raise_for_status()
    html = resp.text
    initial = extract_initial_data(html)
    ytcfg = extract_ytcfg(html)
    if not dig(initial, "contents", "liveChatRenderer"):
        msg = _plain(dig(initial, "contents", "messageRenderer", "text")) or "Chat is not available for this video."
        raise ChatUnavailable(msg, "disabled")
    logged_in = str(ytcfg.get("LOGGED_IN")).lower() == "true"
    return ChatPage(video_id, ytcfg, initial, logged_in=logged_in, cookies=dict(cookies or {}))
