"""Optional cloud models for the message box (free tiers of Gemini, Groq and OpenRouter).

All three speak the OpenAI chat API. Model names change often, so by default the best
available model is picked from the provider's own model list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from .client import LLMClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    base_url: str
    key_url: str  # where to get a free API key
    # Substrings in order of preference when picking a model automatically.
    prefer: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    only: str = ""  # model ids must contain this (OpenRouter: ":free")
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        "gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://aistudio.google.com/apikey",
        prefer=("gemini-flash-latest", "flash"),
        exclude=("lite", "image", "tts", "audio", "live", "embedding", "thinking", "8b", "exp", "learnlm", "gemma"),
        note="Free tier: limits are set per project (see AI Studio). Google may use free-tier prompts to "
             "improve its models.",
    ),
    "groq": Provider(
        "groq", "Groq", "https://api.groq.com/openai/v1", "https://console.groq.com/keys",
        prefer=("kimi-k2", "qwen3", "qwen", "gpt-oss-120b", "llama-3.3-70b", "llama-4"),
        exclude=("whisper", "guard", "tts", "playai", "compound", "embedding", "orpheus", "safeguard"),
        note="Free tier: about 1,000 requests a day per model, no card needed. Very fast.",
    ),
    "openrouter": Provider(
        "openrouter", "OpenRouter (free models)", "https://openrouter.ai/api/v1", "https://openrouter.ai/keys",
        prefer=("deepseek-chat", "deepseek-v3", "kimi-k2", "qwen3", "gemini", "llama-3.3-70b", "gpt-oss-120b"),
        exclude=("r1", "coder", "vision", "-vl", "embed", "distill"),
        only=":free",
        note="Free models: 50 requests a day (1,000 after a one-time $10 credit purchase), 20 a minute. "
             "Free and stealth models log prompts.",
    ),
}


def provider(key: str) -> Provider | None:
    return PROVIDERS.get(key)


def reasoning_params(provider_key: str, model: str) -> dict:
    """Keep 'thinking' models fast: their reasoning would add seconds to every message."""
    m = model.lower()
    if provider_key == "gemini":
        return {"reasoning_effort": "none" if "2.5" in m else "minimal"}
    if provider_key == "groq":
        if "gpt-oss" in m:
            return {"reasoning_effort": "low", "include_reasoning": False}
        if "qwen" in m:
            return {"reasoning_effort": "none", "reasoning_format": "hidden"}
        return {}
    if provider_key == "openrouter":
        return {"reasoning": {"effort": "low", "exclude": True}}
    return {}


def _version(model_id: str) -> tuple[float, ...]:
    nums = re.findall(r"\d+(?:\.\d+)?", model_id)
    return tuple(float(n) for n in nums[:2]) or (0.0,)


def pick_model(ids: list[str], p: Provider) -> str | None:
    """Best model for chat translation from a provider's model list."""
    cands = []
    for raw in ids:
        mid = raw.removeprefix("models/")
        low = mid.lower()
        if p.only and p.only not in low:
            continue
        if any(x in low for x in p.exclude):
            continue
        rank = next((i for i, pref in enumerate(p.prefer) if pref in low), None)
        if rank is None:
            continue
        preview = "preview" in low
        cands.append((rank, preview, tuple(-v for v in _version(low)), len(mid), mid))
    if not cands:
        return None
    return min(cands)[-1]


async def list_models(p: Provider, api_key: str, timeout: float = 15.0) -> list[str]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{p.base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
    if r.status_code in (401, 403):
        raise PermissionError(f"{p.name} rejected the API key ({r.status_code}).")
    r.raise_for_status()
    data = r.json().get("data") or []
    return [str(d.get("id")) for d in data if d.get("id")]


@dataclass
class CloudModel:
    """A provider + key; resolves the model lazily and builds a client for it."""

    provider: Provider
    api_key: str
    model: str = ""  # empty = pick automatically
    _client: LLMClient | None = field(default=None, repr=False)

    @property
    def label(self) -> str:
        name = self.provider.name.split(" (")[0]
        return f"{name} · {self.model}" if self.model else name

    async def client(self) -> LLMClient:
        if self._client is None:
            if not self.model:
                ids = await list_models(self.provider, self.api_key)
                picked = pick_model(ids, self.provider)
                if not picked:
                    raise LookupError(f"No suitable model found on {self.provider.name}.")
                log.info("%s: using %s", self.provider.name, picked)
                self.model = picked
            self._client = LLMClient(
                self.provider.base_url, model=self.model, api_key=self.api_key, local=False,
                extra_body=reasoning_params(self.provider.key, self.model), timeout=30.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
