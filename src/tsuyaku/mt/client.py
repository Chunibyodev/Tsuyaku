"""Async client for an OpenAI-compatible chat endpoint (llama-server, Ollama, LM Studio...).

Requests are streamed so subtitles can show words as they are generated, and so a
cancelled request (e.g. the composer text changed) stops generation on the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import IntEnum

import httpx

log = logging.getLogger(__name__)

_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


class Priority(IntEnum):
    SUBTITLE = 0
    COMPOSE = 1
    CHAT = 2


@dataclass
class LLMResult:
    text: str
    first_token_s: float
    total_s: float
    tokens: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PriorityGate:
    """Subtitles and the composer go straight through; chat batches wait until no subtitle
    is being translated, and only one chat batch runs at a time."""

    def __init__(self) -> None:
        self._high = 0
        self._cond: asyncio.Condition | None = None
        self._low_lock: asyncio.Lock | None = None

    def _ensure(self) -> None:
        if self._cond is None:
            self._cond = asyncio.Condition()
            self._low_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def high(self) -> AsyncIterator[None]:
        self._ensure()
        self._high += 1
        try:
            yield
        finally:
            self._high -= 1
            async with self._cond:
                self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def low(self, max_wait: float = 2.5) -> AsyncIterator[None]:
        """Wait for subtitles to finish - but never longer than max_wait, so chat can't starve."""
        self._ensure()
        async with self._low_lock:
            try:
                async with self._cond:
                    await asyncio.wait_for(self._cond.wait_for(lambda: self._high == 0), max_wait)
            except TimeoutError:
                pass
            yield

    @property
    def busy_high(self) -> bool:
        return self._high > 0


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text).strip()


class LLMClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str = "local",
        api_key: str = "",
        temperature: float = 0.2,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        local: bool = True,
        extra_body: dict | None = None,
        style: str = "chat",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        # "chat" (instruction model) or "translation" (dedicated translation model): which
        # prompts the translators use with this server.
        self.style = style
        # local = llama-server (or another server on this PC): gets llama.cpp extensions and
        # JSON-schema output, and never goes through a system proxy.
        self.local = local
        # Provider-specific fields (e.g. reasoning settings); dropped if the server rejects them.
        self.extra_body = dict(extra_body or {})
        self.gate = PriorityGate()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers=headers,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            trust_env=not local,  # never send local traffic through a system proxy
            transport=transport,
        )
        self.stats: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        try:
            r = await self._client.get(f"{root}/health", timeout=3.0)
            if r.status_code == 200:
                return True
            r = await self._client.get(f"{self.base_url}/models", timeout=3.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def chat(
        self,
        messages: list[dict],
        *,
        priority: Priority = Priority.SUBTITLE,
        max_tokens: int = 160,
        temperature: float | None = None,
        json_schema: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        stop: list[str] | None = None,
        gated: bool = True,
        top_p: float | None = None,
    ) -> LLMResult:
        """gated=False: the caller already holds the priority gate (several requests of one batch)."""
        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature if temperature is None else temperature,
            "top_p": top_p or 0.9,
            "max_tokens": max_tokens,
        }
        if self.local:
            # llama-server extensions (ignored by other local servers):
            body["cache_prompt"] = True
            body["chat_template_kwargs"] = {"enable_thinking": False}
            body["stream_options"] = {"include_usage": True}
            if self.style == "translation":
                body.update(top_p=top_p or 0.6, top_k=20, repeat_penalty=1.05)  # Hy-MT's recommended sampling
        if stop:
            body["stop"] = stop
        if json_schema is not None and self.local:
            # Cloud models are asked for JSON in the prompt instead: schema support varies.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": json_schema, "strict": True},
            }

        if not gated:
            gate = contextlib.nullcontext()
        elif priority == Priority.CHAT:
            gate = self.gate.low()
        else:
            gate = self.gate.high()
        async with gate:
            if not self.extra_body:
                return await self._stream(body, on_token)
            try:
                return await self._stream({**body, **self.extra_body}, on_token)
            except LLMError as exc:
                if exc.status != 400:
                    raise
                log.info("%s rejected %s; retrying without them", self.base_url, list(self.extra_body))
                self.extra_body = {}
                return await self._stream(body, on_token)

    async def _stream(self, body: dict, on_token: Callable[[str], None] | None) -> LLMResult:
        t0 = time.perf_counter()
        first: float | None = None
        parts: list[str] = []
        tokens = prompt_tokens = cached = 0
        in_think = False
        try:
            async with self._client.stream("POST", f"{self.base_url}/chat/completions", json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise LLMError(f"LLM server returned {resp.status_code}: {detail}", resp.status_code)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event.get("error"), dict):  # some providers report errors mid-stream
                        err = event["error"]
                        code = err.get("code")
                        raise LLMError(f"LLM error: {err.get('message') or err}", code if isinstance(code, int) else None)
                    usage = event.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        tokens = usage.get("completion_tokens", tokens)
                    timings = event.get("timings")
                    if timings:
                        cached = timings.get("cache_n", cached)
                    for choice in event.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if not delta:
                            continue
                        if first is None:
                            first = time.perf_counter() - t0
                        parts.append(delta)
                        # Hide any <think> block from live updates.
                        joined = "".join(parts)
                        if "<think>" in joined and "</think>" not in joined:
                            in_think = True
                            continue
                        if in_think:
                            in_think = False
                        if on_token:
                            on_token(strip_thinking(joined))
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        total = time.perf_counter() - t0
        text = strip_thinking("".join(parts))
        return LLMResult(text, first if first is not None else total, total, tokens, prompt_tokens, cached)
