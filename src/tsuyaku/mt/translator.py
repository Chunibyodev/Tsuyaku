"""The three translation jobs: live subtitles, chat, and the composer."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .. import jp
from ..cues import Cue, CueStore
from . import prompts
from .client import LLMClient, LLMError, Priority
from .cloud import CloudModel
from .glossary import Entry, format_block

log = logging.getLogger(__name__)

_LABEL = re.compile(r"^\s*(english|en|subtitle|translation)\s*[:：]\s*", re.I)
# "Here's the exact translation: **…**" and a trailing "*(or …)*" from chattier models.
_PREAMBLE = re.compile(r"^\s*here(?:['’]s| is)\b[^:：\n]{0,40}\btranslation\b[^:：\n]{0,20}[:：]\s*", re.I)
_ASIDE = re.compile(r"\s*\*\([^)]*\)\*\s*$")
_QUOTES = "\"'“”「」『』"


def tidy_line(text: str) -> str:
    """Make model output fit for a subtitle: one line, no labels or wrapping quotes."""
    text = text.strip()
    if "\n" in text:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        text = " ".join(lines)
    text = _ASIDE.sub("", _PREAMBLE.sub("", text))
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        text = text[2:-2].strip()
    text = _LABEL.sub("", text)
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text


@dataclass
class StreamMeta:
    title: str = ""
    channel: str = ""
    channel_id: str = ""


# ============================================================================ subtitles


class SubtitleTranslator:
    """Translates recognised lines in order, keeping recent lines as conversation context.

    Context is kept as alternating user/assistant turns that only grow (trimmed in blocks),
    so each request shares a long prefix with the previous one and llama-server only has
    to process the new line.
    """

    def __init__(
        self,
        client: LLMClient,
        cues: CueStore,
        *,
        context_lines: int = 6,
        on_update: Callable[[Cue], None] | None = None,
        stream_tokens: bool = True,
    ) -> None:
        self.client = client
        self.cues = cues
        self.context_lines = max(0, context_lines)
        self.on_update = on_update or (lambda cue: None)
        self.stream_tokens = stream_tokens
        self.meta = StreamMeta()
        self.terms: list[Entry] = []
        self.system = prompts.subtitle_system("", "", "")
        self.history: list[tuple[str, str]] = []
        self.queue: asyncio.Queue[Cue] = asyncio.Queue()
        self.enabled = True
        # Live: drop/merge lines when behind. Archives/files are processed ahead of playback,
        # so every line is translated in order.
        self.live = True
        self.errors = 0
        self.last_error = ""

    def set_context(self, meta: StreamMeta, terms: list[Entry]) -> None:
        self.meta, self.terms = meta, terms
        self.system = prompts.subtitle_system(meta.title, meta.channel, format_block(terms))
        self.history.clear()

    def submit(self, cue: Cue) -> None:
        self.queue.put_nowait(cue)

    def _messages(self, line: str) -> list[dict]:
        if self.client.style == prompts.TRANSLATION:
            history = self.history[-self.context_lines:] if self.context_lines else []
            text = prompts.mt_subtitle(line, history, self.meta.title, self.meta.channel, self.terms)
            return [{"role": "user", "content": text}]
        msgs = [{"role": "system", "content": self.system}]
        for src, dst in self.history:
            msgs.append({"role": "user", "content": src})
            msgs.append({"role": "assistant", "content": dst})
        msgs.append({"role": "user", "content": line})
        return msgs

    def _remember(self, src: str, dst: str) -> None:
        if self.context_lines == 0:
            return
        self.history.append((src, dst))
        if len(self.history) > self.context_lines + 4:
            self.history = self.history[-self.context_lines:]

    # When translation falls behind: lines older than this are shown in Japanese instead,
    # and at most MAX_MERGE pending lines are translated together.
    MAX_LAG_S = 10.0
    MAX_MERGE = 3

    async def warmup(self) -> None:
        """Process the system prompt once so the first real line is fast."""
        try:
            await self.client.chat(self._messages("はい"), priority=Priority.SUBTITLE, max_tokens=1)
        except LLMError as exc:
            log.debug("warm-up failed: %s", exc)

    async def run(self) -> None:
        while True:
            cue = await self.queue.get()
            if not self.live:
                if cue.status == "pending" and self.enabled:
                    await self.translate(cue)
                continue
            batch = [cue]
            while not self.queue.empty():
                batch.append(self.queue.get_nowait())
            batch = [c for c in batch if c.status == "pending"]
            if not batch:
                continue
            if len(batch) > 1:
                now = time.time()
                fresh = [c for c in batch if now - (c.arrived_at or now) < self.MAX_LAG_S] or batch[-1:]
                keep = fresh[-self.MAX_MERGE:]
                for c in batch:
                    if c not in keep:
                        c.status = "error"  # too late to be useful live: show the Japanese
                        self.on_update(c)
                cue = self.cues.merge(keep) if len(keep) > 1 else keep[0]
                self.on_update(cue)
            if not self.enabled:
                continue
            await self.translate(cue)

    async def translate(self, cue: Cue) -> None:
        line = cue.jp + ("…" if cue.forced_cut and not cue.jp.endswith(("…", "。", "？", "！", "?", "!")) else "")
        cue.status = "translating"
        cue.mt_started_at = time.time()

        def on_token(partial: str) -> None:
            if not cue.mt_first_at:
                cue.mt_first_at = time.time()
            if self.stream_tokens:
                cue.en = tidy_line(partial)
                self.on_update(cue)

        try:
            result = await self.client.chat(
                self._messages(line), priority=Priority.SUBTITLE, max_tokens=120, on_token=on_token,
                stop=["\n\n"],
            )
        except LLMError as exc:
            self.errors += 1
            self.last_error = str(exc)
            log.warning("subtitle translation failed: %s", exc)
            cue.status = "error"
            cue.mt_done_at = time.time()
            self.on_update(cue)
            return
        cue.en = tidy_line(result.text)
        cue.status = "done" if cue.en else "error"
        cue.mt_done_at = time.time()
        if not cue.mt_first_at:
            cue.mt_first_at = cue.mt_done_at
        if cue.en:
            self._remember(line, cue.en)
        self.on_update(cue)


# ============================================================================ chat


@dataclass
class ChatJob:
    msg_id: str
    text: str
    received_at: float
    force: bool = False


SKIPPED = "\x00skipped"  # sentinel result: too old, translate on demand


class ChatTranslator:
    """Batches chat messages; canned slang and a cache handle the common ones for free."""

    def __init__(
        self,
        client: LLMClient,
        on_result: Callable[[str, str | None], None],
        *,
        max_batch: int = 10,
        max_age_s: float = 20.0,
        cache_size: int = 4000,
    ) -> None:
        self.client = client
        self.on_result = on_result
        self.max_batch = max_batch
        self.max_age_s = max_age_s
        self.meta = StreamMeta()
        self.terms: list[Entry] = []
        self.system = prompts.chat_system("", "", "")
        self.cache: OrderedDict[str, str] = OrderedDict()
        self.cache_size = cache_size
        self.queue: asyncio.Queue[ChatJob] = asyncio.Queue()
        self.enabled = True
        self.translated = 0
        self.cache_hits = 0
        self.skipped = 0
        self.in_flight = 0
        self.last_batch: tuple[int, float] | None = None  # (messages, seconds) of the latest request

    def set_context(self, meta: StreamMeta, terms: list[Entry]) -> None:
        self.meta, self.terms = meta, terms
        self.system = prompts.chat_system(meta.title, meta.channel, format_block(terms))

    def submit(self, msg_id: str, text: str, *, force: bool = False) -> None:
        """Queue a message. Returns immediately; results arrive through on_result."""
        if not text.strip():
            self.on_result(msg_id, None)
            return
        quick = jp.quick_chat_translation(text)
        if quick:
            self.on_result(msg_id, quick)
            return
        if not jp.is_japanese(text):
            self.on_result(msg_id, None)  # nothing to translate
            return
        key = jp.normalize(text)
        if key in self.cache:
            self.cache.move_to_end(key)
            self.cache_hits += 1
            self.on_result(msg_id, self.cache[key])
            return
        self.queue.put_nowait(ChatJob(msg_id, text, time.monotonic(), force))

    def _remember(self, src: str, dst: str) -> None:
        self.cache[jp.normalize(src)] = dst
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            # Give a burst of messages a moment to arrive so they share one request.
            await asyncio.sleep(0.12)
            jobs = [job]
            while not self.queue.empty() and len(jobs) < self.max_batch * 4:
                jobs.append(self.queue.get_nowait())
            now = time.monotonic()
            fresh: list[ChatJob] = []
            for j in jobs:
                if not self.enabled:
                    continue
                if not j.force and now - j.received_at > self.max_age_s:
                    self.skipped += 1
                    self.on_result(j.msg_id, SKIPPED)
                    continue
                key = jp.normalize(j.text)
                if key in self.cache:
                    self.on_result(j.msg_id, self.cache[key])
                    continue
                fresh.append(j)
            # Newest first when we're behind: what's on screen matters most.
            if len(fresh) > self.max_batch:
                fresh.sort(key=lambda j: (not j.force, -j.received_at))
                for j in fresh[self.max_batch:]:
                    self.queue.put_nowait(j)
                fresh = fresh[: self.max_batch]
            if fresh:
                await self._translate_batch(fresh)

    async def _translate_batch(self, jobs: list[ChatJob]) -> None:
        # Identical messages in one burst ("かわいい" x10) are translated once.
        unique: list[str] = []
        for j in jobs:
            if j.text not in unique:
                unique.append(j.text)
        t0 = time.monotonic()
        self.in_flight = len(jobs)
        try:
            results = await self.translate_texts(unique)
        except (LLMError, ValueError) as exc:
            log.warning("chat translation failed: %s", exc)
            for j in jobs:
                self.on_result(j.msg_id, SKIPPED)
            return
        finally:
            self.in_flight = 0
        self.last_batch = (len(jobs), round(time.monotonic() - t0, 2))
        log.info("chat: %d message(s) translated in %.1f s, %d waiting, %d skipped as too old so far",
                 len(jobs), self.last_batch[1], self.queue.qsize(), self.skipped)
        mapping = dict(zip(unique, results, strict=True))
        for j in jobs:
            out = mapping.get(j.text) or None
            if out:
                self._remember(j.text, out)
                self.translated += 1
            self.on_result(j.msg_id, out)

    # A translation model gets one message per request; this many run at once, which leaves a
    # server slot free for subtitles.
    MT_PARALLEL = 2

    async def translate_texts(self, texts: list[str]) -> list[str]:
        if self.client.style == prompts.TRANSLATION:
            return await self._translate_each(texts)
        n = len(texts)
        schema = {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n}
        messages = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
        ]
        max_tokens = 40 + 48 * n
        result = await self.client.chat(messages, priority=Priority.CHAT, max_tokens=max_tokens,
                                        json_schema=schema, temperature=0.2)
        return parse_json_list(result.text, n)

    async def _translate_each(self, texts: list[str]) -> list[str]:
        slots = asyncio.Semaphore(self.MT_PARALLEL)

        async def one(text: str) -> str:
            prompt = prompts.mt_chat(text, self.meta.title, self.meta.channel, self.terms)
            async with slots:
                result = await self.client.chat([{"role": "user", "content": prompt}], priority=Priority.CHAT,
                                                max_tokens=40 + 3 * len(text), temperature=0.2, gated=False)
            return tidy_line(result.text)

        async with self.client.gate.low():
            return list(await asyncio.gather(*(one(t) for t in texts)))


def parse_json_list(text: str, n: int) -> list[str]:
    text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                out = [str(x).strip() if x is not None else "" for x in data]
                if len(out) >= n:
                    return out[:n]
                return out + [""] * (n - len(out))
        except json.JSONDecodeError:
            pass
    # Fallback: numbered or plain lines.
    lines = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) == n:
        return lines
    raise ValueError(f"could not parse {n} translations from model output: {text[:200]!r}")


# ============================================================================ composer


@dataclass
class ComposeVersion:
    ja: str
    back: str = ""  # independent English back-translation, to check the meaning


@dataclass
class ComposeResult:
    english: str
    versions: list[ComposeVersion] = field(default_factory=list)
    done: bool = False
    source: str = ""  # which model wrote it
    note: str = ""  # e.g. why the cloud model was skipped

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class ComposeTranslator:
    """English -> Japanese for the message box: a first version (streamed), more versions in
    other wordings, and an independent back-translation of each so the meaning can be checked.

    Uses the cloud model when one is configured and falls back to the local one."""

    CLOUD_COOLDOWN_S = 600.0  # after a rate limit / bad key, use the local model for a while

    def __init__(self, client: LLMClient | None, cloud: CloudModel | None = None) -> None:
        self.client = client
        self.cloud = cloud
        self.meta = StreamMeta()
        self.terms: list[Entry] = []
        self.cache: OrderedDict[tuple, ComposeResult] = OrderedDict()
        self._cloud_skip_until = 0.0
        self.cloud_note = ""

    def set_context(self, meta: StreamMeta, terms: list[Entry]) -> None:
        if (meta.title, meta.channel, terms) != (self.meta.title, self.meta.channel, self.terms):
            self.cache.clear()
        self.meta = meta
        self.terms = terms

    @property
    def glossary(self) -> str:
        return format_block(self.terms)

    @property
    def available(self) -> bool:
        return self.client is not None or self.cloud is not None

    def describe(self) -> str:
        if self.cloud and time.monotonic() >= self._cloud_skip_until:
            return self.cloud.label
        return "local model" if self.client else "no model"

    # ----------------------------------------------------------------- versions
    async def compose(
        self, english: str, tone: str = "casual", recent: list[str] | None = None,
        on_update: Callable[[ComposeResult], None] | None = None, retry: int = 0, versions: int = 3,
    ) -> ComposeResult:
        english = english.strip()
        key = (english, tone, retry, versions)
        if key in self.cache:
            result = self.cache[key]
            if on_update:
                on_update(copy.deepcopy(result))
            return result
        targets: list[str] = []
        if self.cloud and time.monotonic() >= self._cloud_skip_until:
            targets.append("cloud")
        if self.client:
            targets.append("local")
        if not targets:
            raise LLMError("No translation model is available (see Settings → Translation).")
        note = ""
        last: Exception | None = None
        for which in targets:
            try:
                if which == "cloud":
                    assert self.cloud is not None
                    client, label = await self.cloud.client(), self.cloud.label
                else:
                    assert self.client is not None
                    client, label = self.client, "local model"
                result = ComposeResult(english, source=label, note=note)
                await self._compose_with(client, result, tone, recent, on_update, retry, versions)
            except (LLMError, httpx.HTTPError, LookupError, PermissionError) as exc:
                last = exc
                if which == "cloud" and self.cloud:
                    status = getattr(exc, "status", None)
                    if status in (401, 403, 429) or isinstance(exc, (PermissionError, LookupError)):
                        self._cloud_skip_until = time.monotonic() + self.CLOUD_COOLDOWN_S
                    reason = "rate limit reached" if status == 429 else _short_error(exc)
                    note = f"{self.cloud.provider.name}: {reason} — used the local model"
                    self.cloud_note = note
                    log.warning("cloud compose failed (%s); falling back", exc)
                continue
            self.cache[key] = result
            if len(self.cache) > 200:
                self.cache.popitem(last=False)
            return result
        message = _short_error(last) or "translation failed"
        raise LLMError(f"{message} ({note})" if note and note not in message else message)

    async def _compose_with(
        self, client: LLMClient, result: ComposeResult, tone: str, recent: list[str] | None,
        on_update: Callable[[ComposeResult], None] | None, retry: int, versions: int,
    ) -> None:
        def emit() -> None:
            if on_update:
                on_update(copy.deepcopy(result))

        def partial(text: str) -> None:
            result.versions = [ComposeVersion(text)]
            emit()

        first = await self._first(client, result.english, tone, recent, partial, retry)
        if not first:
            raise LLMError("The model returned an empty translation.")
        result.versions = [ComposeVersion(first)]
        emit()

        async def check_first() -> None:
            try:
                result.versions[0].back = await self._back(client, first)
                emit()
            except LLMError as exc:
                log.info("back-translation failed: %s", exc)

        async def more() -> None:
            if versions <= 1:
                return
            try:
                alts = await self._alternatives(client, result.english, tone, first, versions - 1)
            except (LLMError, ValueError) as exc:
                log.info("alternative versions failed: %s", exc)
                return
            if not alts:
                return
            result.versions.extend(ComposeVersion(a) for a in alts)
            emit()
            try:
                backs = await self._back_many(client, alts)
            except (LLMError, ValueError) as exc:
                log.info("back-translation of alternatives failed: %s", exc)
                return
            for v, back in zip(result.versions[1:], backs, strict=False):
                v.back = back
            emit()

        await asyncio.gather(check_first(), more())
        result.done = True
        emit()

    async def _first(self, client: LLMClient, english: str, tone: str, recent: list[str] | None,
                     on_partial: Callable[[str], None] | None, retry: int) -> str:
        temperature = 0.15 + 0.35 * min(retry, 2)
        text = await self._to_japanese_with(client, english, tone, recent, on_partial, temperature)
        if jp.looks_chinese(text):
            # Qwen models occasionally answer in Chinese; ask once more, explicitly.
            text = await self._to_japanese_with(
                client, english, tone, recent, on_partial, 0.3,
                extra="Answer in natural Japanese using hiragana/katakana, not Chinese.")
        return text

    async def _alternatives(self, client: LLMClient, english: str, tone: str, first: str, n: int) -> list[str]:
        if client.style == prompts.TRANSLATION:
            return await self._alternatives_sampled(client, english, tone, first, n)
        system = prompts.compose_system(tone, self.meta.title, self.meta.channel, self.glossary)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompts.ALTERNATIVES.format(english=english, first=first, n=n)},
        ]
        schema = {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n}
        res = await client.chat(messages, priority=Priority.COMPOSE, max_tokens=80 + 90 * n,
                                temperature=0.6, json_schema=schema)
        out: list[str] = []
        for text in parse_json_list(res.text, n):
            text = tidy_line(text)
            if text and text != first and text not in out and not jp.looks_chinese(text) and jp.is_japanese(text):
                out.append(text)
        return out

    async def _alternatives_sampled(self, client: LLMClient, english: str, tone: str, first: str, n: int) -> list[str]:
        """A translation model can't be asked for other wordings: sample the same request again."""
        messages = [{"role": "user", "content": prompts.mt_compose(english, tone, self.terms)}]
        results = await asyncio.gather(*(
            client.chat(messages, priority=Priority.COMPOSE, max_tokens=160, temperature=1.0, top_p=0.95)
            for _ in range(n + 1)))  # one spare: samples often repeat
        out: list[str] = []
        for res in results:
            text = tidy_line(res.text)
            if text and text != first and text not in out and not jp.looks_chinese(text) and jp.is_japanese(text):
                out.append(text)
        return out[:n]

    async def _back(self, client: LLMClient, japanese: str) -> str:
        if client.style == prompts.TRANSLATION:
            messages = [{"role": "user", "content": prompts.mt_back(japanese.strip(), self.terms)}]
        else:
            messages = [
                {"role": "system", "content": prompts.BACK_SYSTEM},
                {"role": "user", "content": japanese.strip()},
            ]
        result = await client.chat(messages, priority=Priority.COMPOSE, max_tokens=160, temperature=0.1)
        return tidy_line(result.text)

    async def _back_many(self, client: LLMClient, texts: list[str]) -> list[str]:
        if client.style == prompts.TRANSLATION:
            return list(await asyncio.gather(*(self._back(client, t) for t in texts)))
        n = len(texts)
        schema = {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n}
        messages = [
            {"role": "system", "content": prompts.BACK_MANY_SYSTEM},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
        ]
        res = await client.chat(messages, priority=Priority.COMPOSE, max_tokens=60 + 80 * n,
                                temperature=0.1, json_schema=schema)
        return [tidy_line(t) for t in parse_json_list(res.text, n)]

    # ----------------------------------------------------------------- single version (classic composer)
    async def to_japanese(
        self, english: str, tone: str = "casual", recent: list[str] | None = None,
        on_partial: Callable[[str], None] | None = None, retry: int = 0,
    ) -> str:
        """One version with the local model. retry > 0 asks for a different wording."""
        if self.client is None:
            raise LLMError("The local translator is not running.")
        return await self._first(self.client, english, tone, recent, on_partial, retry)

    async def _to_japanese_with(
        self, client: LLMClient, english: str, tone: str, recent: list[str] | None,
        on_partial: Callable[[str], None] | None, temperature: float, extra: str = "",
    ) -> str:
        if client.style == prompts.TRANSLATION:
            messages = [{"role": "user", "content": prompts.mt_compose(english, tone, self.terms)}]
            result = await client.chat(
                messages, priority=Priority.COMPOSE, max_tokens=160, temperature=temperature,
                top_p=0.95 if temperature > 0.3 else None,  # ↻: a wider pick gives other wording
                on_token=(lambda t: on_partial(tidy_line(t))) if on_partial else None,
            )
            return tidy_line(result.text)
        system = prompts.compose_system(tone, self.meta.title, self.meta.channel, self.glossary)
        if extra:
            system += "\n- " + extra
        user = english.strip()
        if recent:
            context = "\n".join(f"- {line}" for line in recent[-3:] if line)
            user = f"(What the streamer just said, for context:\n{context})\n\nMy message:\n{user}"
        messages = [{"role": "system", "content": system}]
        for en, ja in prompts.COMPOSE_EXAMPLES.get(tone, prompts.COMPOSE_EXAMPLES["casual"]):
            messages.append({"role": "user", "content": en})
            messages.append({"role": "assistant", "content": ja})
        messages.append({"role": "user", "content": user})
        result = await client.chat(
            messages, priority=Priority.COMPOSE, max_tokens=160, temperature=temperature,
            on_token=(lambda t: on_partial(tidy_line(t))) if on_partial else None,
        )
        return tidy_line(result.text)

    async def back_translate(self, japanese: str) -> str:
        if self.client is None:
            raise LLMError("The local translator is not running.")
        return await self._back(self.client, japanese)


def _short_error(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return text if len(text) < 140 else text[:137] + "…"
