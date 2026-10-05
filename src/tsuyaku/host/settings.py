"""What the extension's settings page can see and change.

Settings live in Tsuyaku's config.toml (and the glossary in glossary.json); the page only edits
the fields listed in EDITABLE, everything else in the file stays as it is.
"""

from __future__ import annotations

import dataclasses

from ..config import Config
from ..mt.cloud import PROVIDERS
from ..mt.glossary import Entry, Glossary


@dataclasses.dataclass(frozen=True)
class ModelPreset:
    label: str
    repo: str
    file: str
    gpu_gb: float  # GPU memory for the model and its context (GiB, approximate; speech model not included)
    group: str
    note: str = ""


# Translation models, grouped by the GPU they need together with the Whisper speech model.
# Quality notes come from JP-TL-Bench (Japanese<->English, shisa.ai), the Hy-MT2 report and a
# side-by-side run of the same stream lines, chat and messages (docs/MODELS.md).
_SMALL, _MID, _LARGE, _HUGE = "8 GB GPUs", "10–12 GB GPUs", "16 GB GPUs", "24 GB GPUs"
LLM_PRESETS = [
    ModelPreset("Qwen3-4B Instruct 2507 · Q5_K_M (default)", "unsloth/Qwen3-4B-Instruct-2507-GGUF",
                "Qwen3-4B-Instruct-2507-Q5_K_M.gguf", 4.4, _SMALL, "Fast, good subtitles."),
    ModelPreset("Gemma 4 E4B · Q4_K_M", "unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_K_M.gguf", 5.7, _SMALL,
                "Newer general model at about the default's speed; translated noticeably better than Qwen3-4B "
                "in a side-by-side test, message box included."),
    ModelPreset("Qwen3-4B Instruct 2507 · Q4_K_M", "unsloth/Qwen3-4B-Instruct-2507-GGUF",
                "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 4.0, _SMALL, "A bit faster, slightly lower quality."),
    ModelPreset("Qwen3.5-4B · Q4_K_M", "unsloth/Qwen3.5-4B-GGUF", "Qwen3.5-4B-Q4_K_M.gguf", 3.4, _SMALL,
                "Newer; better message-box Japanese, a little slower."),
    ModelPreset("Qwen3.5-2B · Q5_K_M (fastest)", "unsloth/Qwen3.5-2B-GGUF", "Qwen3.5-2B-Q5_K_M.gguf", 1.9, _SMALL,
                "Fastest general model; lower quality."),
    ModelPreset("Hy-MT2-1.8B · Q8_0 (translation model)", "tencent/Hy-MT2-1.8B-GGUF", "Hy-MT2-1.8B-Q8_0.gguf",
                2.8, _SMALL, "Made only for translating, very fast; makes clearly more mistakes than the 7B."),
    ModelPreset("Shisa v2.1 Qwen3-8B · Q4_K_M (tuned for Japanese↔English)", "mradermacher/shisa-v2.1-qwen3-8b-GGUF",
                "shisa-v2.1-qwen3-8b.Q4_K_M.gguf", 6.4, _MID,
                "Qwen3-8B trained for Japanese and English: the most natural subtitles in our test; its "
                "message-box Japanese sometimes adds things (check the back-translation). About 1.7× as long "
                "per line."),
    ModelPreset("Hy-MT2-7B · Q4_K_M (translation model)", "tencent/Hy-MT2-7B-GGUF", "Hy-MT2-7B-Q4_K_M.gguf", 5.9,
                _MID, "Tencent's translation model: the most faithful in our test, both ways, message box "
                "included. About 1.7× as long per line."),
    ModelPreset("Qwen3-8B · Q4_K_M", "unsloth/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", 6.4, _MID,
                "General 8B; the Shisa version above translates better."),
    ModelPreset("Ministral 3 14B Instruct · Q4_K_M", "unsloth/Ministral-3-14B-Instruct-2512-GGUF",
                "Ministral-3-14B-Instruct-2512-Q4_K_M.gguf", 9.6, _LARGE,
                "Best Japanese→English of the models that fit a 16 GB card (on par with Gemini 2.5 Flash on "
                "JP-TL-Bench); good English→Japanese."),
    ModelPreset("Gemma 4 26B-A4B · Q4_K_M", "unsloth/gemma-4-26B-A4B-it-GGUF", "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
                17.5, _HUGE, "Large general model that runs about as fast as a 4B one (mixture of experts). "
                "Strong translation both ways, so also a good message-box model."),
    ModelPreset("Hy-MT2-30B-A3B · Q4_K_M (translation model)", "tencent/Hy-MT2-30B-A3B-GGUF",
                "Hy-MT2-30B-A3B-Q4_K_M.gguf", 18.5, _HUGE,
                "The largest translation model, fast (mixture of experts); tops Tencent's translation "
                "benchmarks."),
]

WHISPER_PRESETS = [
    ("kotoba-whisper v2.0 (Japanese, recommended)", "kotoba-tech/kotoba-whisper-v2.0-faster"),
    ("Whisper large-v3-turbo", "deepdml/faster-whisper-large-v3-turbo-ct2"),
    ("Whisper large-v3", "large-v3"),
    ("Whisper small (low-end PCs)", "small"),
]

EDITABLE: dict[str, tuple[str, ...]] = {
    "asr": ("engine", "whisper_model", "device", "compute_type", "window_s"),
    "vad": ("max_segment_s", "min_silence_ms", "threshold"),
    "llm": ("manage_server", "model_repo", "model_file", "model_path", "gpu_layers", "base_url", "api_key",
            "model_name", "temperature"),
    "subtitles": ("context_lines",),
    "compose": ("provider", "versions", "tone", "debounce_ms", "gemini_key", "gemini_model", "groq_key",
                "groq_model", "openrouter_key", "openrouter_model"),
}
# Changing these reloads the speech model / restarts the translator.
RELOAD_ASR = ("engine", "whisper_model", "device", "compute_type", "window_s")
RESTART_LLM = ("manage_server", "model_repo", "model_file", "model_path", "gpu_layers", "base_url", "api_key",
               "model_name")


def payload(cfg: Config, glossary: Glossary, gpu: tuple[str, float] | None = None) -> dict:
    data = cfg.to_dict()
    return {
        "gpu": {"name": gpu[0], "gb": round(gpu[1], 1)} if gpu else None,
        "settings": {section: {k: data[section][k] for k in keys} for section, keys in EDITABLE.items()},
        "presets": {
            "llm": [{"label": p.label, "repo": p.repo, "file": p.file, "gpuGb": p.gpu_gb, "group": p.group,
                     "note": p.note} for p in LLM_PRESETS],
            "whisper": [{"label": label, "model": model} for label, model in WHISPER_PRESETS],
        },
        "providers": [{"key": p.key, "name": p.name, "keyUrl": p.key_url, "note": p.note}
                      for p in PROVIDERS.values()],
        "glossary": {
            "global": [dataclasses.asdict(e) for e in glossary.global_entries],
            "channels": [{"id": cid, "name": ch.get("name") or cid,
                          "entries": [dataclasses.asdict(e) for e in glossary.channel_entries(cid)]}
                         for cid, ch in glossary.channels.items()],
        },
    }


def _clamp(obj, name: str, lo: float, hi: float) -> None:
    setattr(obj, name, type(getattr(obj, name))(min(hi, max(lo, getattr(obj, name)))))


def apply(cfg: Config, changes: dict) -> tuple[bool, bool, bool]:
    """Copy the editable fields from `changes` into cfg. Returns which parts need applying:
    (speech model, translator, message box)."""
    before = cfg.to_dict()
    allowed = {section: {k: v for k, v in changes[section].items() if k in keys}
               for section, keys in EDITABLE.items() if isinstance(changes.get(section), dict)}
    cfg.update_from_dict(allowed)
    a, v, llm, c = cfg.asr, cfg.vad, cfg.llm, cfg.compose
    if a.engine not in ("whisper", "reazon"):
        a.engine = before["asr"]["engine"]
    if a.device not in ("auto", "cuda", "cpu"):
        a.device = before["asr"]["device"]
    a.whisper_model = a.whisper_model.strip() or before["asr"]["whisper_model"]
    _clamp(a, "window_s", 4, 30)
    _clamp(v, "max_segment_s", 1.5, 8.0)
    _clamp(v, "min_silence_ms", 120, 1200)
    _clamp(v, "threshold", 0.2, 0.9)
    _clamp(llm, "gpu_layers", 0, 999)
    _clamp(llm, "temperature", 0.0, 1.5)
    llm.model_name = llm.model_name.strip() or "local"
    for name in ("model_repo", "model_file", "model_path", "base_url"):
        setattr(llm, name, getattr(llm, name).strip())
    _clamp(cfg.subtitles, "context_lines", 0, 20)
    if c.provider != "local" and c.provider not in PROVIDERS:
        c.provider = "local"
    if c.tone not in ("casual", "polite", "fan"):
        c.tone = "casual"
    _clamp(c, "versions", 1, 3)
    _clamp(c, "debounce_ms", 200, 3000)
    after = cfg.to_dict()
    return (
        any(before["asr"][k] != after["asr"][k] for k in RELOAD_ASR),
        any(before["llm"][k] != after["llm"][k] for k in RESTART_LLM),
        before["compose"] != after["compose"],
    )


def save_glossary(glossary: Glossary, scope: str, name: str, entries: list) -> None:
    """Replace the entries of one scope ("" = every stream, else a channel id) and save."""
    clean = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        jp, en = str(e.get("jp") or "").strip(), str(e.get("en") or "").strip()
        if jp and en:
            clean.append(Entry(jp, en, str(e.get("note") or "").strip()))
    if scope:
        glossary.set_channel_entries(scope, name or scope, clean)
    else:
        glossary.global_entries = clean
    glossary.save()
