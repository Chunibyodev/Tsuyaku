"""Live viewer count, like count and title updates (YouTube's own polling endpoint, no login)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .innertube import ChatPage, dig

log = logging.getLogger(__name__)


@dataclass
class StreamStats:
    viewers: int | None = None
    viewers_text: str = ""  # "4,482 watching now"
    likes: int | None = None
    title: str = ""
    date_text: str = ""  # "Started streaming 23 minutes ago"


def _text(obj) -> str:
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    return "".join(r.get("text", "") for r in obj.get("runs", []))


def _int(value) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def parse_metadata(data: dict) -> StreamStats:
    st = StreamStats()
    for action in data.get("actions") or []:
        if "updateViewershipAction" in action:
            r = dig(action, "updateViewershipAction", "viewCount", "videoViewCountRenderer", default={}) or {}
            st.viewers_text = _text(r.get("viewCount"))
            st.viewers = _int(r.get("originalViewCount")) or _int(_text(r.get("unlabeledViewCountValue")))
        elif "updateTitleAction" in action:
            st.title = _text(dig(action, "updateTitleAction", "title"))
        elif "updateDateTextAction" in action:
            st.date_text = _text(dig(action, "updateDateTextAction", "dateText"))
        elif "updateToggleButtonTextAction" in action:  # older like-count format
            r = action["updateToggleButtonTextAction"]
            if r.get("buttonId") == "TOGGLE_BUTTON_ID_TYPE_LIKE":
                st.likes = _int(_text(r.get("defaultText")))
    for m in dig(data, "frameworkUpdates", "entityBatchUpdate", "mutations", default=[]) or []:
        like = dig(m, "payload", "likeCountEntity")
        if like:
            st.likes = _int(like.get("likeCountIfIndifferentNumber")) or _int(
                dig(like, "likeCountIfIndifferent", "content"))
    return st


class MetadataPoller:
    def __init__(self, video_id: str, on_stats: Callable[[StreamStats], None], *,
                 page_provider: Callable[[], ChatPage | None], interval: float = 10.0) -> None:
        self.video_id = video_id
        self.on_stats = on_stats
        self.page_provider = page_provider
        self.interval = interval
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(15.0))
        self.last: StreamStats | None = None

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self.stop()
        await self._client.aclose()

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            page = self.page_provider()
            if page is None:
                await self._sleep(1.0)
                continue
            try:
                resp = await self._client.post(
                    "https://www.youtube.com/youtubei/v1/updated_metadata",
                    params={"prettyPrint": "false"},
                    json={"context": page.context, "videoId": self.video_id},
                    headers=page.headers(),
                    cookies=page.cookies or None,
                )
                resp.raise_for_status()
                stats = parse_metadata(resp.json())
                if self.last:  # fields absent from this response keep their previous value
                    stats.viewers = stats.viewers if stats.viewers is not None else self.last.viewers
                    stats.viewers_text = stats.viewers_text or self.last.viewers_text
                    stats.likes = stats.likes if stats.likes is not None else self.last.likes
                    stats.title = stats.title or self.last.title
                    stats.date_text = stats.date_text or self.last.date_text
                self.last = stats
                self.on_stats(stats)
                failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                log.debug("metadata poll failed: %s", exc)
            await self._sleep(self.interval * min(6, 1 + failures))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
