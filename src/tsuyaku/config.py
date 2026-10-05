"""User configuration, stored as TOML in the platform config directory.

Every field has a sensible default so a missing or partial config file is fine.
Unknown keys in the file are ignored (forward/backward compatible).
"""

from __future__ import annotations

import dataclasses
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from . import paths

log = logging.getLogger(__name__)


@dataclass
class StreamConfig:
    # Highest video height to request (lower = less bandwidth/decoding work).
    max_height: int = 1080
    # How many segments behind the newest one to start at. 1 = start at the live edge.
    edge_segments: int = 1
    # Playlist poll interval while waiting for the next segment (seconds).
    poll_interval: float = 0.25


@dataclass
class VADConfig:
    threshold: float = 0.5
    # Silence that ends a phrase. Lower = faster subtitles, but more fragmented sentences.
    min_silence_ms: int = 300
    # Hard cap on phrase length: long run-on speech is cut at the quietest point before this.
    max_segment_s: float = 3.0
    min_segment_s: float = 0.25
    pad_ms: int = 120


@dataclass
class ASRConfig:
    # "whisper" (faster-whisper / CTranslate2) or "reazon" (sherpa-onnx ReazonSpeech zipformer)
    engine: str = "whisper"
    whisper_model: str = "kotoba-tech/kotoba-whisper-v2.0-faster"
    # "auto" picks CUDA when available.
    device: str = "auto"
    # "auto" -> int8_float32 on CUDA (Pascal has slow FP16), int8 on CPU.
    compute_type: str = "auto"
    beam_size: int = 1
    # Minimum Whisper input window in seconds. Stock Whisper always pads audio to 30 s; a
    # short window makes the encoder ~5-7x faster for short phrases (text that the model
    # places past the real audio is discarded, which prevents the usual repetition).
    # 30 = stock behaviour.
    window_s: int = 6
    # Feed the previous line as a prompt (helps names/consistency).
    use_context_prompt: bool = True
    cpu_threads: int = 4
    reazon_model: str = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"
    reazon_provider: str = "cpu"


@dataclass
class LLMConfig:
    # Start and manage a local llama-server. Turn off to use your own OpenAI-compatible
    # server (Ollama, LM Studio, vLLM...) at base_url.
    manage_server: bool = True
    base_url: str = "http://127.0.0.1:8765/v1"
    api_key: str = ""
    model_name: str = "local"
    model_repo: str = "unsloth/Qwen3-4B-Instruct-2507-GGUF"
    model_file: str = "Qwen3-4B-Instruct-2507-Q5_K_M.gguf"
    # Absolute path to a GGUF file; overrides model_repo/model_file when set.
    model_path: str = ""
    # Layers on the GPU; 99 = all that fit (the rest stays in system memory, slower).
    gpu_layers: int = 99
    # Total context shared by the slots (subtitles, chat, composer). ~2.7K tokens per slot is
    # plenty; every 1K tokens costs ~150 MB of VRAM with a 4B model.
    ctx_size: int = 8192
    parallel: int = 3
    # llama-server keeps old prompts in system RAM for reuse (its default is 8 GB!).
    cache_ram_mb: int = 256
    port: int = 8765
    extra_args: list[str] = field(default_factory=list)
    # Explicit llama-server executable; empty = the one downloaded by setup.
    server_path: str = ""
    temperature: float = 0.2
    # How to prompt the model: "auto" (by its name), "chat" (instruction models such as Qwen or
    # Gemma) or "translation" (dedicated translation models: Hy-MT).
    prompt_style: str = "auto"


@dataclass
class SubtitleConfig:
    # Previous lines given to the translator as context.
    context_lines: int = 6
    # Show words as they are generated.
    stream_tokens: bool = True


@dataclass
class ChatConfig:
    enabled: bool = True
    translate: bool = True
    # "live" (all messages) or "top" (YouTube's filtered "Top chat")
    mode: str = "live"
    max_batch: int = 10
    # When the translator falls behind, messages older than this are shown untranslated
    # (click to translate).
    max_age_s: float = 20.0


@dataclass
class ComposeConfig:
    debounce_ms: int = 1000
    # "casual", "polite", "fan"
    tone: str = "casual"
    back_translate: bool = True
    # How many versions to suggest (each with its own back-translation).
    versions: int = 3
    # Model for the message box: "local", or a free cloud tier: "gemini", "groq", "openrouter".
    # Falls back to the local model when the cloud fails or hits its limit.
    provider: str = "local"
    gemini_key: str = ""
    gemini_model: str = ""  # empty = pick the best available automatically
    groq_key: str = ""
    groq_model: str = ""
    openrouter_key: str = ""
    openrouter_model: str = ""

    def cloud_key(self, provider: str | None = None) -> str:
        return str(getattr(self, f"{provider or self.provider}_key", "") or "")

    def cloud_model(self, provider: str | None = None) -> str:
        return str(getattr(self, f"{provider or self.provider}_model", "") or "")


@dataclass
class AccountConfig:
    # For the command-line tools (members-only streams): the browser to read your YouTube
    # cookies from (firefox, chrome, edge, brave, ...), or empty.
    cookies_browser: str = ""
    # Netscape-format cookies.txt (alternative to cookies_browser).
    cookies_file: str = ""


@dataclass
class MetaConfig:
    version: int = 2


@dataclass
class Config:
    stream: StreamConfig = field(default_factory=StreamConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    subtitles: SubtitleConfig = field(default_factory=SubtitleConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    compose: ComposeConfig = field(default_factory=ComposeConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    meta: MetaConfig = field(default_factory=MetaConfig)

    # ----------------------------------------------------------------- io
    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or paths.config_file()
        cfg = cls()
        if path.exists():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                cfg.update_from_dict(data)
                cfg._migrate(int((data.get("meta") or {}).get("version", 1)))
            except Exception:  # noqa: BLE001 - a broken config must never stop the app
                log.exception("Could not read %s; using defaults", path)
        return cfg

    def _migrate(self, version: int) -> None:
        """Move users off old defaults that turned out to be bad (saved configs keep them)."""
        if version < 2:
            if self.llm.ctx_size == 16384:
                self.llm.ctx_size = 8192
            if self.llm.parallel == 4:
                self.llm.parallel = 3
        self.meta.version = MetaConfig().version

    def save(self, path: Path | None = None) -> None:
        path = path or paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(tomli_w.dumps(self.to_dict()), encoding="utf-8")
        tmp.replace(path)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def update_from_dict(self, data: dict[str, Any]) -> None:
        for section in dataclasses.fields(self):
            values = data.get(section.name)
            if not isinstance(values, dict):
                continue
            target = getattr(self, section.name)
            for f in dataclasses.fields(target):
                if f.name not in values:
                    continue
                value = values[f.name]
                current = getattr(target, f.name)
                try:
                    if isinstance(current, bool):
                        value = bool(value)
                    elif isinstance(current, int) and not isinstance(current, bool):
                        value = int(value)
                    elif isinstance(current, float):
                        value = float(value)
                    elif isinstance(current, list):
                        value = list(value)
                    elif isinstance(current, str):
                        value = str(value)
                except (TypeError, ValueError):
                    log.warning("Ignoring invalid config value %s.%s=%r", section.name, f.name, value)
                    continue
                setattr(target, f.name, value)

    def copy(self) -> Config:
        clone = Config()
        clone.update_from_dict(self.to_dict())
        return clone
