"""The program Firefox starts when the Tsuyaku extension is switched on.

Firefox launches it through native messaging (`browser.runtime.connectNative`) and closes its
stdin when the extension is switched off; it then stops llama-server and exits. In between it
runs the models:

* subtitles: audio captured from YouTube's player -> VAD -> speech recognition -> translation,
  sent back as cues on the video's own timeline (one pipeline per YouTube tab),
* chat: YouTube's chat messages translated in place (one translator per chat frame),
* message box: English -> Japanese versions with back-translations.

Every message from the extension carries `pid`, the id of the page connection it came from;
replies carry it back so the extension can route them.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import signal
import threading
import time
from collections.abc import Callable

from .. import logsetup
from ..asr.worker import ASRWorker, Transcript
from ..config import Config
from ..cues import Cue, CueStore
from ..download import CancelToken, DownloadCancelled
from ..engine import Engine
from ..mt.glossary import Entry
from ..mt.server import gpu_memory
from ..mt.translator import (
    SKIPPED,
    ChatTranslator,
    ComposeResult,
    ComposeTranslator,
    StreamMeta,
    SubtitleTranslator,
)
from . import settings
from .audio import PageAudio

log = logging.getLogger(__name__)

# Setup components the extension needs (Deno is only for yt-dlp in the command-line tools).
NEEDED = ("asr", "llama-server", "llm-model")
LLM_KEYS = {"llama-server", "llm-model"}

Send = Callable[[dict], None]


def cue_dict(cue: Cue) -> dict:
    return {"id": cue.id, "start": round(cue.start, 3), "end": round(cue.end, 3), "jp": cue.jp,
            "en": cue.en, "status": cue.status}


# ============================================================================ subtitles


class SubtitlePipeline:
    """One video's subtitles: page audio -> VAD + ASR thread -> cues -> translation."""

    def __init__(self, host: Host, pid: int, video: str, meta: StreamMeta) -> None:
        self.host = host
        self.pid = pid
        self.video = video
        self.meta = meta
        self.cues = CueStore(max_cues=1000)
        self.audio = PageAudio()
        self.worker: ASRWorker | None = None
        self.translator: SubtitleTranslator | None = None
        self._tasks: list[asyncio.Task] = []
        self.closed = False
        self.last_audio = 0.0

    def push(self, pcm: bytes, rate: int, video_time: float, speed: float, continuous: bool,
             volume: float) -> None:
        engine = self.host.engine
        if engine.asr is None:
            return
        if self.worker is None:
            cfg = self.host.cfg
            self.worker = ASRWorker(engine.asr, cfg.vad, self._on_transcript_thread,
                                    use_prompt=cfg.asr.use_context_prompt)
        self.last_audio = time.time()
        chunk = self.audio.push(pcm, rate, video_time, speed, continuous, self.last_audio, volume)
        if chunk is not None:
            self.worker.push(chunk)

    def _on_transcript_thread(self, t: Transcript) -> None:  # ASR thread
        self.host.loop.call_soon_threadsafe(self._on_transcript, t)

    def _on_transcript(self, t: Transcript) -> None:
        if self.closed:
            return
        cue = Cue(
            start=self.audio.to_video(t.start), end=self.audio.to_video(t.end), jp=t.text,
            forced_cut=t.forced_cut, arrived_at=t.arrived_at, closed_at=t.closed_at,
            asr_done_at=t.asr_done_at, asr_seconds=t.asr_seconds,
        )
        self.cues.add(cue)
        translator = self.ensure_translator()
        if translator:
            translator.submit(cue)
        else:
            cue.status = "error"  # no translator: show the Japanese
        self.send_cue(cue)

    def ensure_translator(self) -> SubtitleTranslator | None:
        llm = self.host.engine.llm
        if self.translator is None and llm is not None and not self.closed:
            cfg = self.host.cfg.subtitles
            tr = SubtitleTranslator(llm, self.cues, context_lines=cfg.context_lines,
                                    on_update=self.send_cue, stream_tokens=cfg.stream_tokens)
            tr.set_context(self.meta, self.host.glossary_terms(self.meta))
            tr.live = True  # the page plays in real time: drop/merge lines when behind
            self.translator = tr
            self._tasks.append(asyncio.ensure_future(tr.warmup()))
            self._tasks.append(asyncio.ensure_future(tr.run()))
        return self.translator

    def set_meta(self, meta: StreamMeta) -> None:
        self.meta = meta
        if self.translator:
            self.translator.set_context(meta, self.host.glossary_terms(meta))

    def send_cue(self, cue: Cue) -> None:
        if not self.closed and cue.status != "merged":
            self.host.send({"type": "cue", "pid": self.pid, "video": self.video, "cue": cue_dict(cue)})

    def recent(self, n: int = 3) -> list[str]:
        return [c.display_text for c in self.cues.recent(n) if c.display_text]

    def stats(self) -> dict:
        lat = self.cues.latency_summary()
        return {
            "lines": len(self.cues.all()),
            "hearing": time.time() - self.last_audio < 2.0,
            "backlog": self.worker.backlog() if self.worker else 0,
            "asr": lat.get("asr"), "translation": lat.get("mt_total"), "total": lat.get("pipeline"),
        }

    def close(self) -> None:
        self.closed = True
        if self.worker:
            self.worker.stop()
        for t in self._tasks:
            t.cancel()


class WatchPage:
    """A YouTube tab (its top frame). The pipeline is rebuilt for every video it shows."""

    def __init__(self, host: Host, pid: int, tab: int) -> None:
        self.host = host
        self.pid = pid
        self.tab = tab
        self.video = ""
        self.meta = StreamMeta()
        self.pipeline: SubtitlePipeline | None = None

    def set_video(self, msg: dict) -> None:
        video = str(msg.get("video") or "")
        meta = StreamMeta(str(msg.get("title") or ""), str(msg.get("channel") or ""),
                          str(msg.get("channelId") or ""))
        if meta.channel_id:
            self.host.engine.glossary.ensure_channel(meta.channel_id, meta.channel)
        if video != self.video:
            self.stop()
            self.video = video
        self.meta = meta
        if self.pipeline:
            self.pipeline.set_meta(meta)
        self.host.context_changed(self.tab, self.video)

    def audio(self, msg: dict) -> None:
        if not self.video:
            return
        try:
            pcm = base64.b64decode(msg.get("pcm") or "", validate=True)
        except (binascii.Error, ValueError):
            return
        if self.pipeline is None:
            self.pipeline = SubtitlePipeline(self.host, self.pid, self.video, self.meta)
        self.pipeline.push(pcm, int(msg.get("rate") or 0), float(msg.get("t") or 0.0),
                           float(msg.get("speed") or 1.0), bool(msg.get("cont", True)),
                           float(msg.get("vol", 1.0)))

    def stop(self) -> None:
        if self.pipeline:
            self.pipeline.close()
            self.pipeline = None


# ============================================================================ chat


class ChatFrame:
    """YouTube's chat in one frame (inside a watch page, or a pop-out)."""

    def __init__(self, host: Host, pid: int, tab: int, video: str) -> None:
        self.host = host
        self.pid = pid
        self.tab = tab
        self.video = video
        self.translator: ChatTranslator | None = None
        self._task: asyncio.Task | None = None
        self.compose_seq = -1
        self.compose_task: asyncio.Task | None = None

    def _ensure(self) -> ChatTranslator | None:
        llm = self.host.engine.llm
        if llm is None:
            return None
        if self.translator is None:
            cfg = self.host.cfg.chat
            self.translator = ChatTranslator(llm, self._result, max_batch=cfg.max_batch, max_age_s=cfg.max_age_s)
            self.update_context()
            self._task = asyncio.ensure_future(self.translator.run())
        return self.translator

    def update_context(self) -> None:
        if self.translator:
            meta = self.host.meta_for(self.tab, self.video)
            self.translator.set_context(meta, self.host.glossary_terms(meta))

    def translate(self, items: list, force: bool) -> None:
        tr = self._ensure()
        for item in items:
            if not isinstance(item, dict):
                continue
            key, text = str(item.get("key") or ""), str(item.get("text") or "")
            if not key:
                continue
            if tr is None:
                # Translator not running (yet): the page asks again once it is ready.
                self.host.send({"type": "chat", "pid": self.pid, "key": key, "text": "", "state": "off"})
            else:
                tr.submit(key, text, force=force)

    def _result(self, key: str, text: str | None) -> None:
        if text == SKIPPED:
            state, text = "skipped", ""
        elif text:
            state = "done"
        else:
            state, text = "none", ""
        self.host.send({"type": "chat", "pid": self.pid, "key": key, "text": text, "state": state})

    def stats(self) -> dict:
        tr = self.translator
        if tr is None:
            return {"running": False, "translated": 0, "waiting": 0, "skipped": 0, "batch": None}
        return {"running": True, "translated": tr.translated + tr.cache_hits,
                "waiting": tr.queue.qsize() + tr.in_flight, "skipped": tr.skipped, "batch": tr.last_batch}

    # ----------------------------------------------------------------- message box
    def compose(self, seq: int, text: str, tone: str, retry: int) -> None:
        self.cancel_compose(None)
        self.compose_seq = seq
        composer = self.host.engine.composer
        if not composer.available:
            self._composed(seq, {"error": "The translator is still starting…", "done": True})
            return
        meta = self.host.meta_for(self.tab, self.video)
        composer.set_context(meta, self.host.glossary_terms(meta))
        recent = self.host.recent_for(self.tab, self.video)
        if tone in ("casual", "polite", "fan"):
            self.host.cfg.compose.tone = tone
        versions = max(1, min(3, self.host.cfg.compose.versions))

        def update(result: ComposeResult) -> None:
            self._composed(seq, result.to_dict())

        async def run() -> None:
            try:
                await composer.compose(text, self.host.cfg.compose.tone, recent, on_update=update,
                                       retry=retry, versions=versions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - shown in the message box panel
                log.info("compose failed: %s", exc)
                message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                self._composed(seq, {"error": message, "done": True})

        self.compose_task = asyncio.ensure_future(run())

    def cancel_compose(self, seq: int | None) -> None:
        if seq is not None and seq != self.compose_seq:
            return
        if self.compose_task and not self.compose_task.done():
            self.compose_task.cancel()  # stops generation on the server too

    def _composed(self, seq: int, result: dict) -> None:
        if seq == self.compose_seq:
            self.host.send({"type": "composed", "pid": self.pid, "seq": seq, "result": result})

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        self.cancel_compose(None)


# ============================================================================ host


class Host:
    def __init__(self, cfg: Config, send: Send, engine: Engine | None = None) -> None:
        self.cfg = cfg
        self.send = send
        self.loop = asyncio.get_running_loop()
        self.engine = engine or Engine(cfg)
        self.engine.loop = self.loop
        self.engine.add_status_listener(self._engine_status)
        self.texts = {"asr": "", "llm": ""}
        self.pages: dict[int, WatchPage] = {}
        self.chats: dict[int, ChatFrame] = {}
        self.missing: dict[str, str] = {}  # setup component key -> title
        self.progress: dict | None = None
        self.setup_error = ""
        self._cancel = CancelToken()
        self._installing = False
        self._last_progress = 0.0
        self._attempted: set[str] = set()  # "asr" / "llm" loaded at least once
        self._loading: set[str] = set()  # loaders running now
        self._gpu: tuple[str, float] | tuple[()] | None = None  # (name, GiB), () = no NVIDIA GPU

    # ----------------------------------------------------------------- start / stop
    def start(self) -> None:
        """Load what is installed; what is missing waits for `setup` (the popup's button)."""
        self.missing = self._find_missing()
        self._load_ready_parts()
        self.send_status()

    def _find_missing(self, changed: frozenset[str] = frozenset()) -> dict[str, str]:
        """Setup components (key -> title) that still have to be downloaded. A part that is
        running counts as present, unless its settings just `changed` ("asr" / "llm")."""
        from .. import setup

        have = set()
        if self.engine.asr is not None and "asr" not in changed:
            have.add("asr")
        if self.engine.llm is not None and "llm" not in changed:
            have |= LLM_KEYS
        return {c.key: c.title for c in setup.components(self.cfg)
                if c.key in NEEDED and c.key not in have and not c.is_ready()}

    def _load_ready_parts(self) -> None:
        if "asr" not in self.missing and self.engine.asr is None:
            self._start_loader("asr")
        if not LLM_KEYS & self.missing.keys() and self.engine.llm is None:
            self._start_loader("llm")

    def _start_loader(self, part: str) -> None:
        """(Re)load the speech model ("asr") or (re)start the translator ("llm") in a thread."""
        if part in self._loading:
            return
        self._loading.add(part)
        self._attempted.add(part)
        target = self.engine.load_asr if part == "asr" else self.engine.start_llm
        old = self.engine.llm if part == "llm" else None
        if part == "asr":
            self.engine.asr_idle.clear()  # now, so a translator starting alongside waits for it

        def run() -> None:
            try:
                target()
            finally:
                self.loop.call_soon_threadsafe(self._loaded, part, old)

        threading.Thread(target=run, name=f"load-{part}", daemon=True).start()

    def _loaded(self, part: str, old_llm=None) -> None:
        self._loading.discard(part)
        e = self.engine
        if part == "asr":
            for page in self.pages.values():
                page.stop()  # rebuilt with the new model on the next audio
        else:
            if e.llm_error:
                e.llm = e.composer.client = None  # a failed restart leaves no working server
            self._use_llm(e.llm)
            if old_llm is not None and old_llm is not e.llm:
                asyncio.ensure_future(old_llm.aclose())
        self.send_status()

    def _use_llm(self, llm) -> None:
        """Point every running translator at the (new) translation server."""
        for page in self.pages.values():
            if page.pipeline:
                if page.pipeline.translator and llm is not None:
                    page.pipeline.translator.client = llm
                page.pipeline.ensure_translator()
        for chat in self.chats.values():
            if chat.translator and llm is not None:
                chat.translator.client = llm

    async def close(self) -> None:
        self._cancel.cancel()
        for page in self.pages.values():
            page.stop()
        for chat in self.chats.values():
            chat.close()
        try:
            await asyncio.wait_for(self.engine.aclose(), 2.0)
        except (Exception, TimeoutError):  # noqa: BLE001 - shutting down anyway
            log.debug("engine close", exc_info=True)
        await asyncio.to_thread(self.engine.shutdown)

    # ----------------------------------------------------------------- status
    def _engine_status(self, component: str, text: str) -> None:  # loader threads
        self.loop.call_soon_threadsafe(self._on_engine_status, component, text)

    def _on_engine_status(self, component: str, text: str) -> None:
        if component in self.texts:
            self.texts[component] = text
        self.send_status()

    def _state(self, name: str) -> str:
        if name == "asr" and "asr" in self.missing or name == "llm" and LLM_KEYS & self.missing.keys():
            return "missing"
        if name in self._loading:
            return "loading"
        if (self.engine.asr if name == "asr" else self.engine.llm) is not None:
            return "ready"
        return "error" if name in self._attempted else "off"

    def status(self) -> dict:
        e = self.engine
        errors = {"asr": e.asr_error, "llm": e.llm_error}
        parts = {}
        for name in ("asr", "llm"):
            state = self._state(name)
            text = self.texts[name]
            if state == "missing":
                text = "Not downloaded yet"
            elif state == "error":
                text = (errors[name] or text or "Failed").splitlines()[0]
            elif state == "loading" and not text:
                text = "Starting…"
            parts[name] = {"state": state, "text": text}
        c = self.cfg.compose
        return {
            "type": "status",
            **parts,
            "compose": e.composer.available,
            "missing": list(self.missing.values()),
            "installing": self._installing,
            "progress": self.progress,
            "setupError": self.setup_error,
            "config": {"debounceMs": c.debounce_ms, "versions": max(1, min(3, c.versions)), "tone": c.tone},
        }

    def send_status(self) -> None:
        self.send(self.status())

    # ----------------------------------------------------------------- settings page
    def settings_request(self, kind: str, msg: dict) -> None:
        pid = msg.get("pid")
        if kind == "settings":
            asyncio.ensure_future(self._send_settings(pid))
        elif kind == "saveSettings":
            self.send({"type": "saved", "pid": pid, **self.save_settings(msg.get("settings") or {})})
        elif kind == "saveGlossary":
            settings.save_glossary(self.engine.glossary, str(msg.get("scope") or ""), str(msg.get("name") or ""),
                                   msg.get("entries") or [])
            for page in self.pages.values():
                if page.pipeline:
                    page.pipeline.set_meta(page.meta)
            for chat in self.chats.values():
                chat.update_context()
            self.send({"type": "saved", "pid": pid, "ok": True, "restarting": [], "glossary": True})
        else:
            asyncio.ensure_future(self._cloud_request(kind, msg))

    async def _send_settings(self, pid) -> None:
        if self._gpu is None:  # asked once: nvidia-smi takes a moment
            self._gpu = await asyncio.to_thread(gpu_memory) or ()
        self.send({"type": "settings", "pid": pid, **settings.payload(self.cfg, self.engine.glossary, self._gpu or None)})

    def save_settings(self, changes: dict) -> dict:
        asr_changed, llm_changed, compose_changed = settings.apply(self.cfg, changes)
        try:
            self.cfg.save()
        except OSError as exc:
            return {"ok": False, "error": f"Could not save the settings: {exc}"}
        e = self.engine
        if e.llm is not None:
            e.llm.temperature = self.cfg.llm.temperature
        if compose_changed:
            e.apply_compose_settings()
        changed = frozenset({"asr"} if asr_changed else set()) | frozenset({"llm"} if llm_changed else set())
        self.missing = self._find_missing(changed)
        restarting = []
        if asr_changed:
            if "asr" in self.missing:
                e.asr = None  # the new model has to be downloaded first
                for page in self.pages.values():
                    page.stop()
            else:
                self._start_loader("asr")
                restarting.append("speech model")
        if llm_changed:
            if LLM_KEYS & self.missing.keys():
                # The new model has to be downloaded first: stop the old one.
                threading.Thread(target=e.shutdown, name="stop-llm", daemon=True).start()
                e.llm = e.composer.client = None
            else:
                self._start_loader("llm")
                restarting.append("translator")
        self.send_status()
        return {"ok": True, "restarting": restarting, "missing": list(self.missing.values())}

    async def _cloud_request(self, kind: str, msg: dict) -> None:
        from ..mt.cloud import PROVIDERS, CloudModel, list_models, pick_model

        pid = msg.get("pid")
        prov = PROVIDERS.get(str(msg.get("provider") or ""))
        key = str(msg.get("key") or "").strip()
        try:
            if kind == "cloudModels":
                if prov is None or not key:
                    raise ValueError("Choose a cloud provider and paste your API key first.")
                ids = await list_models(prov, key)
                usable = sorted({i.removeprefix("models/") for i in ids
                                 if (not prov.only or prov.only in i) and not any(x in i.lower() for x in prov.exclude)})
                self.send({"type": "cloudModels", "pid": pid, "ok": True, "models": usable,
                           "best": pick_model(ids, prov) or ""})
                return
            if prov is None:
                if self.engine.llm is None:
                    raise ValueError("The local translator isn't running yet.")
                tr = ComposeTranslator(self.engine.llm)
            else:
                if not key:
                    raise ValueError("Paste your API key first.")
                tr = ComposeTranslator(None, CloudModel(prov, key, str(msg.get("model") or "")))
            t0 = time.monotonic()
            try:
                result = await tr.compose("Good luck with the stream! I'll be watching.",
                                          str(msg.get("tone") or "casual"), versions=1)
            finally:
                if tr.cloud:
                    await tr.cloud.aclose()
            v = result.versions[0]
            self.send({"type": "testCompose", "pid": pid, "ok": True, "ja": v.ja, "back": v.back,
                       "source": result.source, "seconds": round(time.monotonic() - t0, 1)})
        except Exception as exc:  # noqa: BLE001 - shown on the settings page
            self.send({"type": kind, "pid": pid, "ok": False, "error": str(exc).splitlines()[0][:200] if str(exc)
                       else exc.__class__.__name__})

    def send_stats(self, msg: dict) -> None:
        """What Tsuyaku is doing in one tab (the popup shows it)."""
        tab = int(msg.get("tab", -1))
        page = next((p for p in self.pages.values() if p.tab == tab and p.video), None)
        chats = [c.stats() for c in self.chats.values() if c.tab == tab]
        asr = self.engine.asr
        self.send({
            "type": "stats", "pid": msg.get("pid"), "tab": tab,
            "device": getattr(asr, "device", ""),
            "subtitles": page.pipeline.stats() if page and page.pipeline else ({"lines": 0} if page else None),
            "chat": chats[0] if chats else None,
        })

    # ----------------------------------------------------------------- downloads
    def install(self) -> None:
        if self._installing or not self.missing:
            return
        self._installing = True
        self.setup_error = ""
        self.send_status()
        threading.Thread(target=self._install, name="setup", daemon=True).start()

    def _install(self) -> None:  # setup thread
        from .. import setup

        error = ""
        for comp in setup.components(self.cfg):
            if comp.key not in NEEDED or comp.is_ready():
                continue
            try:
                comp.install(self._progress_thread, self._cancel)
            except DownloadCancelled:
                return
            except Exception as exc:  # noqa: BLE001 - reported in the popup
                log.exception("download of %s failed", comp.key)
                error = f"{comp.title}: {exc}"
                break
        self.loop.call_soon_threadsafe(self._installed, error)

    def _progress_thread(self, done: int, total: int, label: str) -> None:
        now = time.monotonic()
        if now - self._last_progress < 0.25 and done != total:
            return
        self._last_progress = now
        self.loop.call_soon_threadsafe(self._progress, {"label": label, "done": done, "total": total})

    def _progress(self, progress: dict) -> None:
        self.progress = progress
        self.send_status()

    def _installed(self, error: str) -> None:
        self._installing = False
        self.progress = None
        self.setup_error = error
        self.missing = self._find_missing()
        self._load_ready_parts()
        self.send_status()

    # ----------------------------------------------------------------- context
    def glossary_terms(self, meta: StreamMeta) -> list[Entry]:
        return self.engine.glossary.entries_for(meta.channel_id or None)

    def _page_for(self, tab: int, video: str) -> WatchPage | None:
        """The watch page a chat frame belongs to: same tab, else the same video elsewhere."""
        for page in self.pages.values():
            if page.tab == tab and page.video:
                return page
        if video:
            for page in self.pages.values():
                if page.video == video:
                    return page
        return None

    def meta_for(self, tab: int, video: str = "") -> StreamMeta:
        page = self._page_for(tab, video)
        return page.meta if page else StreamMeta()

    def recent_for(self, tab: int, video: str = "") -> list[str]:
        page = self._page_for(tab, video)
        return page.pipeline.recent() if page and page.pipeline else []

    def context_changed(self, tab: int, video: str) -> None:
        for chat in self.chats.values():
            if chat.tab == tab or (video and chat.video == video):
                chat.update_context()

    # ----------------------------------------------------------------- messages
    def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "setup":
            self.install()
            return
        if kind == "status":
            self.send_status()
            return
        if kind == "stats":
            self.send_stats(msg)
            return
        if kind in ("settings", "saveSettings", "saveGlossary", "cloudModels", "testCompose"):
            self.settings_request(kind, msg)
            return
        try:
            pid = int(msg.get("pid"))
        except (TypeError, ValueError):
            log.debug("message without pid: %s", kind)
            return
        tab = int(msg.get("tab", -1))
        if kind == "watch":
            page = self.pages.get(pid)
            if page is None:
                page = self.pages[pid] = WatchPage(self, pid, tab)
            page.set_video(msg)
        elif kind == "audio":
            page = self.pages.get(pid)
            if page:
                page.audio(msg)
        elif kind == "unwatch":
            page = self.pages.get(pid)
            if page:
                page.stop()
                page.video = ""
        elif kind == "chatHello":
            self._chat(pid, tab, msg)
        elif kind == "chat":
            self._chat(pid, tab, msg).translate(msg.get("items") or [], bool(msg.get("force")))
        elif kind == "compose":
            self._chat(pid, tab, msg).compose(int(msg.get("seq") or 0), str(msg.get("text") or ""),
                                              str(msg.get("tone") or ""), int(msg.get("retry") or 0))
        elif kind == "cancelCompose":
            chat = self.chats.get(pid)
            if chat:
                chat.cancel_compose(int(msg.get("seq") or 0))
        elif kind == "closed":
            page = self.pages.pop(pid, None)
            if page:
                page.stop()
            chat = self.chats.pop(pid, None)
            if chat:
                chat.close()
        else:
            log.debug("unknown message %r", kind)

    def _chat(self, pid: int, tab: int, msg: dict) -> ChatFrame:
        chat = self.chats.get(pid)
        if chat is None:
            chat = self.chats[pid] = ChatFrame(self, pid, tab, str(msg.get("video") or ""))
        return chat


# ============================================================================ entry point


async def serve(cfg: Config, stdin, writer, engine: Engine | None = None) -> None:
    """Run until the browser closes stdin (the extension was switched off)."""
    from . import protocol

    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[dict | None] = asyncio.Queue()

    def read() -> None:  # reader thread
        try:
            while (msg := protocol.read_message(stdin)) is not None:
                loop.call_soon_threadsafe(inbox.put_nowait, msg)
        except Exception:  # noqa: BLE001
            log.exception("reading from the browser failed")
        finally:
            loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=read, name="stdin", daemon=True).start()
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, inbox.put_nowait, None)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: Firefox closes stdin first, then kills the whole process tree

    host = Host(cfg, writer.send, engine)
    host.start()
    try:
        while (msg := await inbox.get()) is not None:
            try:
                host.handle(msg)
            except Exception:  # noqa: BLE001 - one bad message must not stop the host
                log.exception("message %r failed", msg.get("type"))
    finally:
        log.info("browser disconnected; shutting down")
        await host.close()


def main() -> None:
    from . import protocol

    stdin, stdout = protocol.claim_stdio()
    logsetup.setup(console=True)
    log.info("Firefox extension host starting")
    cfg = Config.load()
    asyncio.run(serve(cfg, stdin, protocol.Writer(stdout)))
    log.info("Firefox extension host stopped")
