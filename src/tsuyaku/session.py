"""One watched stream: resolve -> ingest -> speech -> translation, plus chat."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Protocol

from .asr.worker import ASRWorker, Transcript
from .chat.innertube import ChatPage
from .chat.metadata import MetadataPoller, StreamStats
from .chat.models import ChatUpdate
from .chat.reader import ChatReader
from .cookies import CookieSource, load_jar, youtube_cookies
from .cues import Cue, CueStore
from .engine import Engine
from .ingest.audio import AudioChunk, FileAudioReader
from .ingest.live import LiveIngest
from .ingest.resolver import ResolveError, StreamInfo, Track, resolve
from .mt.translator import ChatTranslator, StreamMeta, SubtitleTranslator

log = logging.getLogger(__name__)


class SessionListener(Protocol):
    """All callbacks run on the asyncio loop thread."""

    def on_status(self, text: str, level: str = "info") -> None: ...
    def on_stream_info(self, info: StreamInfo) -> None: ...
    def on_cue(self, cue: Cue) -> None: ...
    def on_chat(self, update: ChatUpdate) -> None: ...
    def on_chat_translation(self, msg_id: str, text: str | None) -> None: ...
    def on_stats(self, stats: StreamStats) -> None: ...
    def on_ended(self, reason: str) -> None: ...


def is_local_file(url: str) -> bool:
    p = Path(url)
    return p.exists() and p.is_file()


def split_hls_info(spec: str) -> StreamInfo:
    """'hls-split:<video.m3u8>|<audio.m3u8>' - separate live tracks (testing/advanced use)."""
    video_url, _, audio_url = spec[len("hls-split:"):].partition("|")
    video = Track(kind="video", url=video_url.strip(), format_id="hls-v", protocol="m3u8_native")
    audio = Track(kind="audio", url=audio_url.strip(), format_id="hls-a", protocol="m3u8_native")
    return StreamInfo(
        video_id="", webpage_url=spec, title="Live HLS", channel="", channel_id="",
        description="", live_status="is_live", video=video, audio=audio,
    )


def direct_hls_info(url: str) -> StreamInfo:
    """A raw .m3u8 media playlist (muxed audio+video), e.g. for testing without YouTube."""
    track = Track(kind="muxed", url=url, format_id="hls", protocol="m3u8_native")
    return StreamInfo(
        video_id="", webpage_url=url, title=Path(url.split("?")[0]).name, channel="", channel_id="",
        description="", live_status="is_live", video=track, audio=track,
    )


class Session:
    def __init__(
        self, engine: Engine, url: str, listener: SessionListener, *, subtitles: bool = True,
        realtime_files: bool = False, chat: bool = True,
    ) -> None:
        self.subtitles = subtitles
        self.chat = chat
        # Headless replay of a local file: feed audio at playback speed, like a live stream.
        self.realtime_files = realtime_files
        self.engine = engine
        self.cfg = engine.cfg
        self.url = url.strip()
        self.listener = listener
        self.loop = asyncio.get_running_loop()
        self.info: StreamInfo | None = None
        self.cues = CueStore()
        self.ingest: LiveIngest | None = None
        self.file_reader: FileAudioReader | None = None
        self.asr_worker: ASRWorker | None = None
        self.chat_reader: ChatReader | None = None
        self.metadata: MetadataPoller | None = None
        self.last_stats: StreamStats | None = None
        self.sub_tr: SubtitleTranslator | None = None
        self.chat_tr: ChatTranslator | None = None
        self.local_file: str | None = None
        self.started_at = time.time()
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self.cookie_source = CookieSource.from_config(self.cfg.account)

    # ----------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.listener.on_status("Opening stream…")
        if is_local_file(self.url):
            return await self._start_local_file()
        if self.url.startswith("hls-split:"):
            info = split_hls_info(self.url)
        elif ".m3u8" in self.url.split("?")[0]:
            info = direct_hls_info(self.url)
        else:
            info = await self._resolve()
        self.info = info
        self.listener.on_stream_info(info)
        meta = StreamMeta(info.title, info.channel, info.channel_id)
        self.engine.glossary.ensure_channel(info.channel_id, info.channel)
        await self._start_translation(meta)
        if self.subtitles:
            await self._start_asr()

        if self.subtitles and info.is_live and info.audio.is_hls:
            if self.sub_tr:
                self.sub_tr.live = True
            self.ingest = LiveIngest(
                info,
                self._on_audio,
                poll_interval=self.cfg.stream.poll_interval,
                edge_segments=self.cfg.stream.edge_segments,
                refresh=self._refresh_info,
            )
            self._spawn(self._run_ingest(), "ingest")
        elif self.subtitles:
            if self.sub_tr:
                self.sub_tr.live = False
            self.file_reader = FileAudioReader(
                info.audio.url, self._on_audio, headers=info.http_headers,
                on_done=lambda: self.loop.call_soon_threadsafe(self._on_reader_done),
            )
            self.file_reader.start()

        if info.video_id and info.is_live:
            if self.cfg.chat.enabled and self.chat:
                self._start_chat(info.video_id)
            self._start_metadata(info.video_id)
        self.listener.on_status(self._ready_text())

    def _ready_text(self) -> str:
        parts = ["Live" if self.info and self.info.is_live else "Playing"]
        if not self.subtitles:
            pass
        elif self.asr_worker is None:
            parts.append("no speech model (see Setup)")
        elif self.sub_tr is None:
            parts.append("subtitles in Japanese only (translator unavailable)")
        else:
            parts.append("English subtitles on")
        return " · ".join(parts)

    async def _start_local_file(self) -> None:
        """Replay a local media file as if it were a stream (testing/demo)."""
        self.local_file = self.url
        info = StreamInfo(
            video_id="", webpage_url=self.url, title=Path(self.url).name, channel="", channel_id="",
            description="", live_status="not_live", video=None,
            audio=Track(kind="muxed", url=self.url, format_id="file", protocol="file"),
        )
        self.info = info
        self.listener.on_stream_info(info)
        await self._start_translation(StreamMeta(info.title))
        if self.sub_tr:
            self.sub_tr.live = self.realtime_files
        await self._start_asr()
        self.file_reader = FileAudioReader(
            self.url, self._on_audio, realtime=self.realtime_files,
            on_done=lambda: self.loop.call_soon_threadsafe(self._on_reader_done),
        )
        self.file_reader.start()
        self.listener.on_status(self._ready_text())

    async def _resolve(self) -> StreamInfo:
        self.listener.on_status("Looking up stream (yt-dlp)…")
        try:
            return await asyncio.to_thread(
                resolve, self.url, max_height=self.cfg.stream.max_height, cookies=self.cookie_source
            )
        except ResolveError as exc:
            signin = ("set [account] cookies_browser = \"firefox\" in config.toml to use your Firefox "
                      "sign-in, then try again.")
            hint = {
                "bot_check": "YouTube wants a signed-in session: " + signin,
                "members_only": "Members-only stream: " + signin,
                "login": "This video needs a signed-in account: " + signin,
                "upcoming": "The stream hasn't started yet. Try again when it goes live.",
                "cookies": "Could not read browser cookies. Firefox works best; or export a cookies.txt.",
                "rate_limited": "YouTube is rate limiting this connection; wait a minute and retry.",
            }.get(exc.kind, "")
            raise ResolveError(f"{exc}\n{hint}".strip(), exc.kind) from exc

    async def _refresh_info(self) -> StreamInfo:
        info = await self._resolve()
        self.info = info
        return info

    async def _start_translation(self, meta: StreamMeta) -> None:
        if not self.engine.llm_ready.is_set():
            self.listener.on_status("Waiting for the translator to start…")
            await asyncio.to_thread(self.engine.llm_ready.wait)
        llm = self.engine.llm
        glossary = self.engine.glossary.entries_for(meta.channel_id)
        if llm is None:
            self.listener.on_status(
                f"Translator unavailable ({self.engine.llm_error or 'not configured'}); showing Japanese.", "warning"
            )
            return
        self.sub_tr = SubtitleTranslator(
            llm, self.cues,
            context_lines=self.cfg.subtitles.context_lines,
            on_update=self.listener.on_cue,
            stream_tokens=self.cfg.subtitles.stream_tokens,
        )
        self.sub_tr.set_context(meta, glossary)
        self._spawn(self.sub_tr.warmup(), "warmup")
        self._spawn(self.sub_tr.run(), "subtitles")
        if self.chat:
            self.chat_tr = ChatTranslator(
                llm, self.listener.on_chat_translation,
                max_batch=self.cfg.chat.max_batch, max_age_s=self.cfg.chat.max_age_s,
            )
            self.chat_tr.set_context(meta, glossary)
            self.chat_tr.enabled = self.cfg.chat.translate
            self._spawn(self.chat_tr.run(), "chat-translate")

    async def _start_asr(self) -> None:
        if not self.engine.asr_ready.is_set():
            self.listener.on_status("Waiting for the speech model to load…")
            await asyncio.to_thread(self.engine.asr_ready.wait)
        if self.engine.asr is None:
            self.listener.on_status(f"Speech model unavailable: {self.engine.asr_error}", "error")
            return
        self.asr_worker = ASRWorker(
            self.engine.asr, self.cfg.vad, self._on_transcript_thread,
            use_prompt=self.cfg.asr.use_context_prompt,
        )

    def _start_chat(self, video_id: str) -> None:
        cookies = {}
        if self.cookie_source:
            try:
                cookies = youtube_cookies(load_jar(self.cookie_source))
            except Exception as exc:  # noqa: BLE001
                self.listener.on_status(f"Could not load YouTube cookies for chat: {exc}", "warning")
        self.chat_reader = ChatReader(
            video_id, self.listener.on_chat, mode=self.cfg.chat.mode, cookies=cookies,
            on_status=lambda s: self.listener.on_status(s),
        )
        self._spawn(self.chat_reader.run(), "chat")

    def _start_metadata(self, video_id: str) -> None:
        started = time.monotonic()
        fallback = ChatPage(video_id, {}, {})

        def page() -> ChatPage | None:
            if self.chat_reader and self.chat_reader.page:
                return self.chat_reader.page
            # No chat page (chat disabled/failed): a bare web context works for public streams.
            return fallback if time.monotonic() - started > 8 or not self.chat_reader else None

        def on_stats(stats: StreamStats) -> None:
            self.last_stats = stats
            self.listener.on_stats(stats)

        self.metadata = MetadataPoller(video_id, on_stats, page_provider=page)
        self._spawn(self.metadata.run(), "metadata")

    async def _run_ingest(self) -> None:
        assert self.ingest is not None
        try:
            await self.ingest.run()
            if not self._closed:
                self.listener.on_ended("The stream has ended.")
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                log.exception("ingest failed")
                self.listener.on_ended(f"Stream error: {exc}")

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)

        def done(t: asyncio.Task) -> None:
            if t.cancelled() or self._closed:
                return
            exc = t.exception()
            if exc:
                log.error("task %s failed: %r", name, exc)
                self.listener.on_status(f"{name} stopped: {exc}", "error")

        task.add_done_callback(done)

    async def stop(self) -> None:
        self._closed = True
        if self.chat_reader:
            await self.chat_reader.aclose()
        if self.metadata:
            await self.metadata.aclose()
        if self.ingest:
            await self.ingest.close()
        if self.file_reader:
            self.file_reader.stop()
        if self.asr_worker:
            self.asr_worker.stop()
        for t in self._tasks:
            t.cancel()

    # ----------------------------------------------------------------- data flow
    def _on_audio(self, chunk: AudioChunk) -> None:  # decoder thread
        if self.asr_worker:
            self.asr_worker.push(chunk)

    def _on_reader_done(self) -> None:
        if self.asr_worker:
            self.asr_worker.flush()

    def _on_transcript_thread(self, t: Transcript) -> None:  # ASR thread
        self.loop.call_soon_threadsafe(self._on_transcript, t)

    def _on_transcript(self, t: Transcript) -> None:
        cue = Cue(
            start=t.start, end=t.end, jp=t.text, forced_cut=t.forced_cut,
            arrived_at=t.arrived_at, closed_at=t.closed_at, asr_done_at=t.asr_done_at,
            asr_seconds=t.asr_seconds,
        )
        self.cues.add(cue)
        if self.sub_tr:
            self.sub_tr.submit(cue)
        else:
            cue.status = "error"  # no translator: show Japanese
        self.listener.on_cue(cue)

    # ----------------------------------------------------------------- user actions
    def request_chat_translation(self, msg_id: str, text: str) -> None:
        if self.chat_tr:
            self.chat_tr.submit(msg_id, text, force=True)

    def update_context(self) -> None:
        """Glossary changed: rebuild translator prompts."""
        if not self.info:
            return
        meta = StreamMeta(self.info.title, self.info.channel, self.info.channel_id)
        glossary = self.engine.glossary.entries_for(meta.channel_id)
        if self.sub_tr:
            self.sub_tr.set_context(meta, glossary)
        if self.chat_tr:
            self.chat_tr.set_context(meta, glossary)

    def idle(self) -> bool:
        """True when every recognised line has been translated (used by the CLI)."""
        if self.file_reader and self.file_reader._thread.is_alive():
            return False
        if self.asr_worker and (self.asr_worker.backlog() or self.asr_worker.busy):
            return False
        return all(c.status in ("done", "error", "merged") for c in self.cues.recent(50))

    def stats(self) -> dict:
        out = dict(self.cues.latency_summary())
        if self.ingest:
            st = self.ingest.stats
            out["segment"] = st.segment_duration
            if st.youtube_latency is not None:
                out["youtube"] = st.youtube_latency
            out["gaps"] = self.ingest.gaps()
            out["live_edge"] = st.live_edge_media_time or 0.0
        if self.asr_worker:
            out["asr_backlog"] = self.asr_worker.backlog()
        if self.chat_tr:
            out["chat_backlog"] = self.chat_tr.queue.qsize()
        return out
