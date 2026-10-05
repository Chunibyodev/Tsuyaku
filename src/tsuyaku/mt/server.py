"""Download and run llama.cpp's `llama-server` (OpenAI-compatible) as a child process."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from .. import download, paths
from ..config import LLMConfig

log = logging.getLogger(__name__)

LLAMA_REPO = "ggml-org/llama.cpp"
# Known-good build used when the GitHub API is unreachable or rate limited.
FALLBACK_TAG = "b11384"


def llama_dir() -> Path:
    return paths.bin_dir() / "llama"


def server_executable(cfg: LLMConfig | None = None) -> Path | None:
    if cfg and cfg.server_path:
        p = Path(cfg.server_path)
        return p if p.exists() else None
    found = download.find_file(llama_dir(), paths.exe_name("llama-server")) if llama_dir().exists() else None
    if found:
        return found
    which = shutil.which("llama-server")
    return Path(which) if which else None


def model_path(cfg: LLMConfig) -> Path:
    if cfg.model_path:
        return Path(cfg.model_path)
    return paths.models_dir() / "llm" / cfg.model_file


def model_ready(cfg: LLMConfig) -> bool:
    return model_path(cfg).exists()


def _nvidia_smi() -> str | None:
    smi = shutil.which("nvidia-smi")
    if not smi and sys.platform == "win32":
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        smi = str(candidate) if candidate.exists() else None
    return smi


def has_nvidia_gpu() -> bool:
    smi = _nvidia_smi()
    if not smi:
        return False
    try:
        out = subprocess.run([smi, "-L"], capture_output=True, text=True, timeout=10, **_no_window())
        return "GPU" in out.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def gpu_info() -> str:
    smi = _nvidia_smi()
    if not smi:
        return ""
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total,driver_version,compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, **_no_window(),
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def gpu_memory() -> tuple[str, float] | None:
    """Name and total memory (GiB) of the first NVIDIA GPU."""
    for line in gpu_info().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                return parts[0], int(parts[1].split()[0]) / 1024
            except ValueError:
                return None
    return None


def _no_window() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def asset_patterns() -> list[str]:
    """Release asset name patterns for this machine, in order of preference."""
    machine = platform.machine().lower()
    if sys.platform == "win32":
        if has_nvidia_gpu():
            # CUDA 12 build: CUDA 13 dropped Pascal (GTX 10xx) support.
            return [r"^llama-b\d+-bin-win-cuda-12\.\d+-x64\.zip$", r"^cudart-llama-bin-win-cuda-12\.\d+-x64\.zip$"]
        return [r"^llama-b\d+-bin-win-vulkan-x64\.zip$"]
    if sys.platform == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        return [rf"^llama-b\d+-bin-macos-{arch}\.(zip|tar\.gz)$"]
    if machine in ("aarch64", "arm64"):
        return [r"^llama-b\d+-bin-ubuntu-arm64\.(zip|tar\.gz)$"]
    # Linux x64: prebuilt binaries are CPU/Vulkan; CUDA users should point server_path at
    # their own build (or use Ollama via base_url).
    if shutil.which("vulkaninfo"):
        return [r"^llama-b\d+-bin-ubuntu-vulkan-x64\.(zip|tar\.gz)$"]
    return [r"^llama-b\d+-bin-ubuntu-x64\.(zip|tar\.gz)$"]


def _fallback_assets(patterns: list[str]) -> list[tuple[str, str]]:
    """Construct asset URLs for FALLBACK_TAG when the API is unavailable."""
    tag = FALLBACK_TAG
    base = f"https://github.com/{LLAMA_REPO}/releases/download/{tag}/"
    names = []
    for p in patterns:
        if "cudart" in p:
            names.append("cudart-llama-bin-win-cuda-12.4-x64.zip")
        elif "win-cuda" in p:
            names.append(f"llama-{tag}-bin-win-cuda-12.4-x64.zip")
        elif "win-vulkan" in p:
            names.append(f"llama-{tag}-bin-win-vulkan-x64.zip")
        elif "macos-arm64" in p:
            names.append(f"llama-{tag}-bin-macos-arm64.tar.gz")
        elif "macos-x64" in p:
            names.append(f"llama-{tag}-bin-macos-x64.tar.gz")
        elif "ubuntu-vulkan" in p:
            names.append(f"llama-{tag}-bin-ubuntu-vulkan-x64.tar.gz")
        elif "ubuntu-arm64" in p:
            names.append(f"llama-{tag}-bin-ubuntu-arm64.tar.gz")
        else:
            names.append(f"llama-{tag}-bin-ubuntu-x64.tar.gz")
    return [(n, base + n) for n in names]


def install_server(progress: download.ProgressFn | None = None, cancel: download.CancelToken | None = None) -> Path:
    patterns = asset_patterns()
    try:
        release = download.github_latest_release(LLAMA_REPO)
        assets = [download.github_find_asset(release, p) for p in patterns]
        if any(a is None for a in assets):
            raise LookupError("asset not found in latest release")
        resolved = [a for a in assets if a]
        log.info("llama.cpp release %s", release.get("tag_name"))
    except (httpx.HTTPError, LookupError, KeyError) as exc:
        log.warning("GitHub API unavailable (%s); using llama.cpp %s", exc, FALLBACK_TAG)
        resolved = _fallback_assets(patterns)

    target = llama_dir()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    tmp = paths.cache_dir()
    for name, url in resolved:
        archive = download.download_file(url, tmp / name, progress=progress, label=name, cancel=cancel)
        download.extract_archive(archive, target)
        download.remove_quietly(archive)
    exe = server_executable()
    if exe is None:
        raise RuntimeError("llama-server was not found in the downloaded archive")
    # The CUDA runtime comes in a separate zip; its DLLs must sit next to llama-server.exe.
    for lib in list(target.rglob("*.dll")):
        if lib.parent != exe.parent and not (exe.parent / lib.name).exists():
            shutil.copy2(lib, exe.parent / lib.name)
    if sys.platform != "win32":
        for f in exe.parent.iterdir():
            if f.is_file() and not f.suffix:
                f.chmod(f.stat().st_mode | 0o111)
    return exe


def install_model(cfg: LLMConfig, progress=None, cancel=None) -> Path:
    if cfg.model_path:
        return Path(cfg.model_path)
    dest_dir = paths.models_dir() / "llm"
    return download.hf_download_file(cfg.model_repo, cfg.model_file, dest_dir, progress=progress, cancel=cancel)


class LlamaServer:
    """Starts llama-server and waits until it is healthy. Stops it on exit."""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.log_path = paths.log_dir() / "llama-server.log"
        self._log_fh = None
        self.last_error = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port}/v1"

    def command(self) -> list[str]:
        exe = server_executable(self.cfg)
        if exe is None:
            raise FileNotFoundError("llama-server is not installed (run setup)")
        model = model_path(self.cfg)
        if not model.exists():
            raise FileNotFoundError(f"LLM model not found: {model}")
        cmd = [
            str(exe),
            "--model", str(model),
            "--host", "127.0.0.1",
            "--port", str(self.cfg.port),
            # 99 = "as many as fit": llama.cpp then keeps what doesn't fit in system memory
            # (for mixture-of-experts models, some experts) instead of failing.
            "--n-gpu-layers", "auto" if self.cfg.gpu_layers >= 99 else str(self.cfg.gpu_layers),
            "--ctx-size", str(self.cfg.ctx_size),
            "--parallel", str(self.cfg.parallel),
            "--cache-ram", str(self.cfg.cache_ram_mb),
            "--jinja",
            "--no-webui",
        ]
        cmd += list(self.cfg.extra_args)
        return cmd

    def start(self, timeout: float = 180.0) -> None:
        if self.proc and self.proc.poll() is None:
            return
        cmd = self.command()
        log.info("starting llama-server: %s", " ".join(cmd))
        self._log_fh = open(self.log_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        env = dict(os.environ)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # never our own stdin (the Firefox host's message pipe)
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(Path(cmd[0]).parent),
            env=env,
            **_no_window(),
        )
        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    self.last_error = self.tail_log()
                    raise RuntimeError(f"llama-server exited (code {self.proc.returncode}).\n{self.last_error}")
                try:
                    r = client.get(f"http://127.0.0.1:{self.cfg.port}/health")
                    if r.status_code == 200:
                        log.info("llama-server is ready")
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
        self.stop()
        raise TimeoutError("llama-server did not become ready in time")

    def tail_log(self, lines: int = 25) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    def start_in_background(self, on_done) -> threading.Thread:
        def run():
            try:
                self.start()
                on_done(None)
            except Exception as exc:  # noqa: BLE001
                on_done(exc)

        t = threading.Thread(target=run, name="llama-server-start", daemon=True)
        t.start()
        return t
