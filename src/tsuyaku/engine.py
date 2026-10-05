"""Long-lived resources shared by every stream: speech model, LLM server/client, glossary and
the message box translator."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from .asr.engines import ASREngine, create_engine, reazon_model_ready, whisper_model_ready
from .config import Config
from .mt import prompts
from .mt.client import LLMClient
from .mt.cloud import CloudModel, provider
from .mt.glossary import Glossary
from .mt.server import LlamaServer, model_ready, server_executable
from .mt.translator import ComposeTranslator

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.glossary = Glossary()
        self.asr: ASREngine | None = None
        self.asr_error = ""
        self.asr_ready = threading.Event()
        self.asr_idle = threading.Event()  # not loading the speech model right now
        self.asr_idle.set()
        self.llm_server: LlamaServer | None = None
        self.llm: LLMClient | None = None
        self.llm_error = ""
        self.llm_ready = threading.Event()
        # The message box translator (local model, optionally a cloud model first).
        self.composer = ComposeTranslator(None, self._cloud_model())
        self._status_listeners: list[Callable[[str, str], None]] = []
        self._asr_lock = threading.Lock()

    # ----------------------------------------------------------------- status
    def add_status_listener(self, fn: Callable[[str, str], None]) -> None:
        self._status_listeners.append(fn)

    def _status(self, component: str, text: str) -> None:
        log.info("[%s] %s", component, text)
        for fn in list(self._status_listeners):
            try:
                fn(component, text)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")

    # ----------------------------------------------------------------- readiness
    def missing_components(self) -> list[str]:
        missing = []
        a = self.cfg.asr
        if a.engine == "reazon" and not reazon_model_ready(a.reazon_model):
            missing.append("asr")
        if a.engine != "reazon" and not whisper_model_ready(a.whisper_model):
            missing.append("asr")
        if self.cfg.llm.manage_server:
            if server_executable(self.cfg.llm) is None:
                missing.append("llama-server")
            if not model_ready(self.cfg.llm):
                missing.append("llm-model")
        return missing

    # ----------------------------------------------------------------- loading
    def load_asr(self) -> None:
        """Blocking. Safe to call again after a settings change (reloads)."""
        with self._asr_lock:
            self.asr_idle.clear()
            self.asr_ready.clear()
            self.asr = None
            self.asr_error = ""
            name = self.cfg.asr.engine
            self._status("asr", f"Loading speech model ({name})…")
            try:
                self.asr = create_engine(self.cfg.asr)
                self._status("asr", f"Speech model ready ({name} on {self.asr.device})")
            except Exception as exc:  # noqa: BLE001
                self.asr_error = str(exc)
                log.exception("speech model failed to load")
                self._status("asr", f"Speech model failed: {exc}")
            finally:
                self.asr_ready.set()
                self.asr_idle.set()

    def start_llm(self) -> None:
        """Blocking: start llama-server if we manage it, then create the client."""
        self.llm_ready.clear()
        self.llm_error = ""
        llm = self.cfg.llm
        base_url = llm.base_url
        try:
            if llm.manage_server:
                if self.llm_server and self.llm_server.running():
                    self.llm_server.stop()
                if not self.asr_idle.is_set():
                    # llama-server sizes itself to the free GPU memory: let the speech model take its share first.
                    self._status("llm", "Waiting for the speech model…")
                    self.asr_idle.wait(timeout=120)
                self.llm_server = LlamaServer(llm)
                self._status("llm", "Starting translator (llama-server)…")
                self.llm_server.start()
                base_url = self.llm_server.base_url
            self.llm = LLMClient(base_url, model=llm.model_name, api_key=llm.api_key, temperature=llm.temperature,
                                 style=prompts.style_for(llm))
            self.composer.client = self.llm
            name = Path(llm.model_path or llm.model_file).stem if llm.manage_server else llm.model_name
            self._status("llm", f"Translator ready ({name})")
        except Exception as exc:  # noqa: BLE001
            self.llm_error = str(exc)
            log.exception("translator failed to start")
            self._status("llm", f"Translator failed: {str(exc).splitlines()[0] if str(exc) else exc}")
        finally:
            self.llm_ready.set()

    def start_background(self) -> None:
        self.asr_idle.clear()
        threading.Thread(target=self.load_asr, name="load-asr", daemon=True).start()
        threading.Thread(target=self.start_llm, name="start-llm", daemon=True).start()

    async def wait_ready(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(asyncio.gather(
            asyncio.to_thread(self.asr_ready.wait), asyncio.to_thread(self.llm_ready.wait)
        ), timeout)

    def _cloud_model(self) -> CloudModel | None:
        c = self.cfg.compose
        p = provider(c.provider)
        key = c.cloud_key()
        if p is None or not key:
            return None
        return CloudModel(p, key, c.cloud_model())

    def apply_compose_settings(self) -> None:
        """Swap the composer's cloud model if the provider, key or model changed."""
        new = self._cloud_model()
        old = self.composer.cloud
        same = (old is None and new is None) or (
            old is not None and new is not None and (old.provider, old.api_key) == (new.provider, new.api_key)
            and (not new.model or old.model == new.model))
        if same:
            return
        self.composer.cloud = new
        self.composer.cache.clear()
        self.composer._cloud_skip_until = 0.0
        if old is not None and self.loop:
            asyncio.run_coroutine_threadsafe(old.aclose(), self.loop)

    loop: asyncio.AbstractEventLoop | None = None  # the backend loop

    async def aclose(self) -> None:
        if self.llm:
            await self.llm.aclose()
        if self.composer.cloud:
            await self.composer.cloud.aclose()

    def shutdown(self) -> None:
        if self.llm_server:
            self.llm_server.stop()
