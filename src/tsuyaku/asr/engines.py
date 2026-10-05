"""Speech recognition engines behind one small interface.

* WhisperEngine  - faster-whisper (CTranslate2). Default model: kotoba-whisper v2.0, a
  Japanese-specialised distillation of Whisper large-v3 (same accuracy, much faster decoder).
* ReazonEngine   - sherpa-onnx ReazonSpeech zipformer. Trained on 35k hours of Japanese TV,
  tiny and fast enough for CPU, no Whisper-style hallucinations, but no punctuation.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .. import download, paths
from ..config import ASRConfig

log = logging.getLogger(__name__)


@dataclass
class ASRResult:
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    elapsed: float = 0.0


class ASREngine(Protocol):
    name: str
    device: str

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> ASRResult: ...


# ----------------------------------------------------------------------------- CUDA setup

_cuda_dirs_added = False


def add_cuda_dll_dirs() -> None:
    """Make pip-installed CUDA 12 runtime libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12)
    visible to CTranslate2. Needed on Windows; harmless elsewhere."""
    global _cuda_dirs_added
    if _cuda_dirs_added:
        return
    _cuda_dirs_added = True
    try:
        import nvidia  # namespace package from the nvidia-* wheels
    except ImportError:
        return
    roots = [Path(p) for p in getattr(nvidia, "__path__", [])]
    for root in roots:
        for sub in root.iterdir():
            for libdir in (sub / "bin", sub / "lib"):
                if not libdir.is_dir():
                    continue
                if sys.platform == "win32":
                    try:
                        os.add_dll_directory(str(libdir))
                    except OSError:
                        pass
                    os.environ["PATH"] = str(libdir) + os.pathsep + os.environ.get("PATH", "")
                else:
                    os.environ["LD_LIBRARY_PATH"] = (
                        str(libdir) + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
                    )
                    # LD_LIBRARY_PATH is read at process start; preload explicitly.
                    import ctypes

                    for lib in sorted(libdir.glob("lib*.so*")):
                        try:
                            ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                        except OSError:
                            pass


def cuda_device_count() -> int:
    add_cuda_dll_dirs()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001
        return 0


def pick_compute_type(device: str, requested: str) -> str:
    if requested and requested != "auto":
        return requested
    import ctranslate2

    try:
        supported = ctranslate2.get_supported_compute_types(device)
    except Exception:  # noqa: BLE001
        supported = {"float32"}
    # Pascal (GTX 10xx) has no fast FP16, so CTranslate2 won't list float16 there and we
    # land on int8_float32. Turing and newer get int8_float16.
    order = ["int8_float16", "int8_float32", "int8", "float32"] if device == "cuda" else ["int8", "float32"]
    for ct in order:
        if ct in supported:
            return ct
    return "float32"


# ----------------------------------------------------------------------------- Whisper


def whisper_model_dir(name: str) -> str:
    """Local folder for a Hugging Face model id, or the name itself (built-in sizes)."""
    if os.path.isdir(name):
        return name
    if "/" in name:
        return str(paths.models_dir() / download.repo_dir_name(name))
    return name


def whisper_model_ready(name: str) -> bool:
    path = whisper_model_dir(name)
    if "/" not in name and not os.path.isdir(name):
        return True  # faster-whisper downloads built-in sizes itself
    return (Path(path) / "model.bin").exists()


class WhisperEngine:
    name = "whisper"

    def __init__(self, cfg: ASRConfig) -> None:
        add_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        device = cfg.device
        if device == "auto":
            device = "cuda" if cuda_device_count() > 0 else "cpu"
        self.device = device
        model_dir = whisper_model_dir(cfg.whisper_model)
        last_error: Exception | None = None
        tried = []
        candidates = [pick_compute_type(device, cfg.compute_type)]
        candidates += [c for c in ("int8_float32", "float32") if c not in candidates]
        for compute_type in candidates:
            try:
                tried.append(compute_type)
                self.model = WhisperModel(
                    model_dir,
                    device=device,
                    compute_type=compute_type,
                    cpu_threads=cfg.cpu_threads,
                    num_workers=1,
                )
                self.compute_type = compute_type
                break
            except (ValueError, RuntimeError) as exc:
                last_error = exc
                log.warning("Whisper with compute_type=%s failed: %s", compute_type, exc)
        else:
            raise RuntimeError(f"Could not load Whisper ({tried}): {last_error}")
        self.cfg = cfg
        self.window_s = max(4, min(30, int(cfg.window_s)))
        tok = self.model.hf_tokenizer
        self._sot = tok.token_to_id("<|startoftranscript|>")
        self._sot_prev = tok.token_to_id("<|startofprev|>")
        self._ja = tok.token_to_id("<|ja|>")
        self._transcribe = tok.token_to_id("<|transcribe|>")
        # Timestamp tokens follow <|notimestamps|>; some tokenizer files (e.g. the built-in sizes)
        # don't list them by name.
        self._ts_begin = tok.token_to_id("<|0.00|>") or tok.token_to_id("<|notimestamps|>") + 1
        self._tok = tok
        log.info("Whisper loaded: %s on %s (%s), window >= %ds", cfg.whisper_model, device,
                 self.compute_type, self.window_s)

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> ASRResult:
        """Transcribe one short phrase.

        Stock Whisper pads every input to 30 s. We pad only to `window_s` (or the phrase
        length + 2 s), which cuts encoder time ~5-7x. With a short window the decoder tends
        to "continue" into the padding and repeat itself, so we decode with timestamps and
        keep only text whose segment starts inside the real audio.
        """
        import ctranslate2

        t0 = time.perf_counter()
        seconds = len(audio) / 16000
        window = int(min(30, max(self.window_s, np.ceil(seconds + 2.0))))
        n = window * 16000
        padded = np.zeros(n, dtype=np.float32)
        padded[: min(len(audio), n)] = audio[:n]
        feats = self.model.feature_extractor(padded, padding=0, chunk_length=30)[:, : window * 100]
        features = ctranslate2.StorageView.from_array(np.ascontiguousarray(feats[None], dtype=np.float32))

        prompt_ids = [self._sot, self._ja, self._transcribe]
        if prompt:
            prev = self._tok.encode(" " + prompt.strip(), add_special_tokens=False).ids[-60:]
            prompt_ids = [self._sot_prev, *prev, *prompt_ids]
        max_new = 10 + int(seconds * 16)

        model = self.model.model
        encoded = model.encode(features, to_cpu=False)
        result = model.generate(
            encoded,
            [prompt_ids],
            beam_size=self.cfg.beam_size,
            max_length=len(prompt_ids) + max_new,
            return_scores=True,
            return_no_speech_prob=True,
            suppress_blank=True,
        )[0]
        text = self._decode_until(result.sequences_ids[0], seconds)
        return ASRResult(
            text=text.strip(),
            avg_logprob=float(result.scores[0]) if result.scores else None,
            no_speech_prob=float(result.no_speech_prob),
            elapsed=time.perf_counter() - t0,
        )

    def _decode_until(self, ids: list[int], seconds: float) -> str:
        """Join timestamped segments that start before the end of the real audio."""
        pieces: list[str] = []
        current: list[int] = []
        start: float | None = None
        for t in ids:
            if t >= self._ts_begin:
                ts = (t - self._ts_begin) * 0.02
                if start is None:
                    if ts >= seconds - 0.1:
                        break
                    start = ts
                else:
                    pieces.append(self._tok.decode(current))
                    current, start = [], None
            elif t < self._sot:
                current.append(t)
        if current and (start is None or start < seconds - 0.1):
            pieces.append(self._tok.decode(current))
        return "".join(pieces)


# ----------------------------------------------------------------------------- ReazonSpeech

REAZON_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{name}.tar.bz2"


def reazon_model_dir(name: str) -> Path:
    return paths.models_dir() / name


def reazon_model_ready(name: str) -> bool:
    d = reazon_model_dir(name)
    return (d / "tokens.txt").exists() and any(d.glob("encoder*.onnx"))


def _pick_onnx(directory: Path, prefix: str) -> str:
    files = sorted(directory.glob(f"{prefix}*.onnx"))
    if not files:
        raise FileNotFoundError(f"{prefix}*.onnx not found in {directory}")
    # Prefer int8 on CPU (smaller & faster), fp32 otherwise.
    int8 = [f for f in files if "int8" in f.name]
    return str((int8 or files)[0])


class ReazonEngine:
    name = "reazon"

    def __init__(self, cfg: ASRConfig) -> None:
        import sherpa_onnx

        d = reazon_model_dir(cfg.reazon_model)
        if not reazon_model_ready(cfg.reazon_model):
            raise FileNotFoundError(f"ReazonSpeech model not downloaded: {d}")
        self.device = cfg.reazon_provider
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=_pick_onnx(d, "encoder"),
            decoder=_pick_onnx(d, "decoder"),
            joiner=_pick_onnx(d, "joiner"),
            tokens=str(d / "tokens.txt"),
            num_threads=cfg.cpu_threads,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            provider=cfg.reazon_provider,
        )
        log.info("ReazonSpeech loaded from %s (%s)", d, cfg.reazon_provider)

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> ASRResult:
        t0 = time.perf_counter()
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, audio)
        self.recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        return ASRResult(text=text, elapsed=time.perf_counter() - t0)


def create_engine(cfg: ASRConfig) -> ASREngine:
    if cfg.engine == "reazon":
        return ReazonEngine(cfg)
    return WhisperEngine(cfg)
