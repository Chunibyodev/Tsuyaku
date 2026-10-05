"""Polls YouTube live chat (no login needed; cookies enable members-only chat)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

from .innertube import ChatPage, ChatUnavailable, dig, fetch_chat_page
from .models import ChatUpdate, parse_actions

log = logging.getLogger(__name__)


class ChatReader:
    def __init__(
        self,
        video_id: str,
        on_update: Callable[[ChatUpdate], None],
        *,
        mode: str = "live",
        cookies: dict[str, str] | None = None,
        on_status: Callable[[str], None] | None = None,
        poll_interval: float = 1.0,
        max_initial: int = 30,
    ) -> None:
        self.video_id = video_id
        self.on_update = on_update
        self.mode = mode
        self.cookies = cookies or {}
        self.on_status = on_status or (lambda s: None)
        self.poll_interval = poll_interval
        self.max_initial = max_initial
        self.page: ChatPage | None = None
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(15.0))
        self.messages_seen = 0
        self.page_ready = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self.stop()
        await self._client.aclose()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                return
            except ChatUnavailable as exc:
                self.on_status(f"Chat unavailable: {exc}")
                if exc.kind in ("disabled",):
                    return
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("chat error: %s", exc)
                self.on_status("Chat connection lost, retrying…")
            await self._sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _session(self) -> None:
        page = await fetch_chat_page(self._client, self.video_id, self.cookies)
        self.page = page
        self.page_ready.set()
        continuation = page.continuation(self.mode)
        if not continuation:
            raise ChatUnavailable("No chat continuation found (chat may be disabled or replay only).", "disabled")
        self.on_status("Chat connected" + (" (signed in)" if page.logged_in else ""))
        first = True
        url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat"
        params = {"prettyPrint": "false"}
        if page.api_key:
            params["key"] = page.api_key
        errors = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                resp = await self._client.post(
                    url,
                    params=params,
                    json={"context": page.context, "continuation": continuation},
                    headers=page.headers(),
                    cookies=self.cookies or None,
                )
                resp.raise_for_status()
                data = resp.json()
                errors = 0
            except (httpx.HTTPError, ValueError) as exc:
                errors += 1
                if errors >= 5:
                    raise
                log.debug("chat poll failed (%s), retrying", exc)
                await self._sleep(min(10.0, 1.5 * errors))
                continue

            lcc = dig(data, "continuationContents", "liveChatContinuation")
            if not lcc:
                raise ChatUnavailable("The live chat has ended.", "ended")
            actions = lcc.get("actions") or []
            update = parse_actions(actions, time.time())
            if first and len(update.added) > self.max_initial:
                update.added = update.added[-self.max_initial:]
            first = False
            if update.added or update.deleted or update.deleted_authors or update.replaced:
                self.messages_seen += len(update.added)
                self.on_update(update)

            next_cont, timeout_ms = None, None
            for c in lcc.get("continuations") or []:
                for key in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
                    if key in c:
                        next_cont = c[key].get("continuation")
                        timeout_ms = c[key].get("timeoutMs")
                        break
                if next_cont:
                    break
            if not next_cont:
                raise ChatUnavailable("The live chat has ended.", "ended")
            continuation = next_cont
            # YouTube suggests long timeouts because its own client gets push updates;
            # we poll about once a second for snappy chat.
            wait = self.poll_interval
            if timeout_ms and timeout_ms < wait * 1000:
                wait = max(0.4, timeout_ms / 1000)
            await self._sleep(max(0.0, wait - (time.monotonic() - t0)))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
