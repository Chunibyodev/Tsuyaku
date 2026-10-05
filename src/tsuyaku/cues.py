"""Subtitle cues: one recognised phrase and its translation, on the stream's media clock."""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class Cue:
    start: float  # media time (seconds)
    end: float
    jp: str
    en: str = ""
    # pending -> translating -> done | error ; merged = folded into another cue
    status: str = "pending"
    forced_cut: bool = False
    id: int = field(default_factory=lambda: next(_ids))
    # Wall-clock timings (time.time()) for the latency display.
    arrived_at: float = 0.0
    closed_at: float = 0.0
    asr_done_at: float = 0.0
    asr_seconds: float = 0.0
    mt_started_at: float = 0.0
    mt_first_at: float = 0.0
    mt_done_at: float = 0.0
    show_jp: bool = False

    @property
    def text_ready(self) -> bool:
        return bool(self.en) or self.status == "error"

    @property
    def display_text(self) -> str:
        """English if available; Japanese if translation failed."""
        return self.en or (self.jp if self.status == "error" else "")

    @property
    def pipeline_seconds(self) -> float | None:
        """Audio arrived -> translation finished."""
        if self.mt_done_at and self.arrived_at:
            return self.mt_done_at - self.arrived_at
        return None

    @property
    def first_word_seconds(self) -> float | None:
        """Audio arrived -> first translated word visible."""
        if self.mt_first_at and self.arrived_at:
            return self.mt_first_at - self.arrived_at
        return None


class CueStore:
    """All cues of the current stream. Thread-safe; the UI reads, the pipeline writes."""

    def __init__(self, max_cues: int = 5000) -> None:
        self.max_cues = max_cues
        self._cues: list[Cue] = []
        self._by_id: dict[int, Cue] = {}
        self._lock = threading.Lock()
        self.version = 0

    def add(self, cue: Cue) -> Cue:
        with self._lock:
            self._cues.append(cue)
            self._by_id[cue.id] = cue
            if len(self._cues) > self.max_cues:
                old = self._cues.pop(0)
                self._by_id.pop(old.id, None)
            self.version += 1
        return cue

    def get(self, cue_id: int) -> Cue | None:
        return self._by_id.get(cue_id)

    def touch(self) -> None:
        self.version += 1

    def clear(self) -> None:
        with self._lock:
            self._cues.clear()
            self._by_id.clear()
            self.version += 1

    def all(self) -> list[Cue]:
        with self._lock:
            return [c for c in self._cues if c.status != "merged"]

    def recent(self, n: int) -> list[Cue]:
        with self._lock:
            out = [c for c in self._cues if c.status != "merged"]
            return out[-n:]

    def merge(self, cues: list[Cue]) -> Cue:
        """Fold several pending cues into the first (used when translation falls behind)."""
        first = cues[0]
        with self._lock:
            first.jp = " ".join(c.jp for c in cues)
            first.end = max(c.end for c in cues)
            first.forced_cut = cues[-1].forced_cut
            first.arrived_at = max(c.arrived_at for c in cues)
            first.asr_done_at = max(c.asr_done_at for c in cues)
            first.closed_at = max(c.closed_at for c in cues)
            first.asr_seconds = sum(c.asr_seconds for c in cues)
            for c in cues[1:]:
                c.status = "merged"
            self.version += 1
        return first

    def latency_summary(self, last: int = 20) -> dict[str, float]:
        cues = [c for c in self.recent(last) if c.mt_done_at]
        if not cues:
            return {}

        def avg(values):
            values = [v for v in values if v is not None]
            return sum(values) / len(values) if values else 0.0

        return {
            "asr": avg(c.asr_seconds for c in cues),
            "mt_first": avg((c.mt_first_at - c.mt_started_at) if c.mt_first_at else None for c in cues),
            "mt_total": avg(c.mt_done_at - c.mt_started_at for c in cues),
            "first_word": avg(c.first_word_seconds for c in cues),
            "pipeline": avg(c.pipeline_seconds for c in cues),
            "vad_wait": avg((c.closed_at - c.arrived_at) if c.closed_at else None for c in cues),
            "age": time.time() - cues[-1].mt_done_at,
        }
