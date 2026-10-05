"""First-run downloads: speech model, LLM + llama-server, Deno (for yt-dlp in the command-line tools)."""

from __future__ import annotations

import logging
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import download, paths
from .asr.engines import (
    REAZON_URL,
    reazon_model_dir,
    reazon_model_ready,
    whisper_model_dir,
    whisper_model_ready,
)
from .config import Config
from .ingest.resolver import deno_path
from .mt import server as llama

log = logging.getLogger(__name__)


@dataclass
class Component:
    key: str
    title: str
    description: str
    size_hint: str
    is_ready: Callable[[], bool]
    install: Callable[[download.ProgressFn | None, download.CancelToken | None], object]
    required: bool = True


# ---------------------------------------------------------------------------- deno


def deno_asset() -> str:
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "deno-x86_64-pc-windows-msvc.zip"
    if sys.platform == "darwin":
        return "deno-aarch64-apple-darwin.zip" if arm else "deno-x86_64-apple-darwin.zip"
    return "deno-aarch64-unknown-linux-gnu.zip" if arm else "deno-x86_64-unknown-linux-gnu.zip"


def deno_ready() -> bool:
    return deno_path() is not None


def install_deno(progress=None, cancel=None) -> Path:
    name = deno_asset()
    url = f"https://github.com/denoland/deno/releases/latest/download/{name}"
    archive = download.download_file(url, paths.cache_dir() / name, progress=progress, label="Deno", cancel=cancel)
    target = paths.bin_dir() / "deno"
    shutil.rmtree(target, ignore_errors=True)
    download.extract_archive(archive, target)
    download.remove_quietly(archive)
    exe = target / paths.exe_name("deno")
    if sys.platform != "win32":
        exe.chmod(0o755)
    return exe


# ---------------------------------------------------------------------------- models


def install_asr(cfg: Config, progress=None, cancel=None) -> Path:
    a = cfg.asr
    if a.engine == "reazon":
        name = a.reazon_model
        url = REAZON_URL.format(name=name)
        archive = download.download_file(url, paths.cache_dir() / f"{name}.tar.bz2", progress=progress,
                                         label="ReazonSpeech model", cancel=cancel)
        download.extract_archive(archive, paths.models_dir())
        download.remove_quietly(archive)
        # The fp32 encoder is large and only needed on GPU; keep it, it's harmless.
        return reazon_model_dir(name)
    if "/" not in a.whisper_model:
        return Path(a.whisper_model)  # built-in size; faster-whisper downloads it itself
    target = Path(whisper_model_dir(a.whisper_model))
    return download.hf_download_repo(a.whisper_model, target, progress=progress, cancel=cancel,
                                     skip=lambda n: n.endswith((".md", ".gitattributes")))


def components(cfg: Config) -> list[Component]:
    a, llm = cfg.asr, cfg.llm
    asr_title = "ReazonSpeech (speech model)" if a.engine == "reazon" else "Whisper speech model"
    asr_name = a.reazon_model if a.engine == "reazon" else a.whisper_model
    out = [
        Component(
            "asr", asr_title, asr_name, "~0.7 GB" if a.engine == "reazon" else "~1.5 GB",
            (lambda: reazon_model_ready(a.reazon_model)) if a.engine == "reazon"
            else (lambda: whisper_model_ready(a.whisper_model)),
            lambda p, c: install_asr(cfg, p, c),
        ),
    ]
    if llm.manage_server:
        out += [
            Component("llama-server", "llama.cpp server", "Runs the translation model on your GPU",
                      "~0.2-0.6 GB", lambda: llama.server_executable(llm) is not None,
                      lambda p, c: llama.install_server(p, c)),
            Component("llm-model", "Translation model", llm.model_file or llm.model_path,
                      "~2.5-3 GB", lambda: llama.model_ready(llm),
                      lambda p, c: llama.install_model(llm, p, c)),
        ]
    # yt-dlp (`tsuyaku run`, `bench --url`) needs a JavaScript runtime for YouTube. The Firefox
    # extension doesn't use it.
    out.append(Component("deno", "Deno", "JavaScript runtime yt-dlp needs for YouTube (command line)",
                         "~40 MB", deno_ready, lambda p, c: install_deno(p, c), required=False))
    return out


def missing(cfg: Config, include_optional: bool = True) -> list[Component]:
    return [c for c in components(cfg) if not c.is_ready() and (c.required or include_optional)]
