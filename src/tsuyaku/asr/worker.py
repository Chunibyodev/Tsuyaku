"""Background thread: audio chunks -> VAD phrases -> recognised Japanese text."""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..config import VADConfig
from ..ingest.audio import AudioChunk
from . import filters
from .engines import ASREngine
from .vad import Segmenter, SileroVAD, Utterance

log = logging.getLogger(__name__)

_ids = itertools.count(1)


@dataclass
class Transcript:
    id: int
    start: float  # media time
    end: float
    text: str
    forced_cut: bool
    arrived_at: float  # wall time the last audio of the phrase reached us
    closed_at: float  # wall time the phrase was closed by the VAD
    asr_done_at: float
    asr_seconds: float
    merged: int = 1

    @property
    def duration(self) -> float:
        return self.end - self.start


class ASRWorker:
    """Owns the segmenter and the engine; everything runs on one thread."""

    MAX_MERGED_SECONDS = 6.0

    def __init__(
        self,
        engine: ASREngine,
        vad_cfg: VADConfig,
        on_transcript: Callable[[Transcript], None],
        *,
        use_prompt: bool = True,
        on_level: Callable[[float], None] | None = None,
    ) -> None:
        self.engine = engine
        self.segmenter = Segmenter(vad_cfg, SileroVAD())
        self.on_transcript = on_transcript
        self.on_level = on_level
        self.use_prompt = use_prompt and engine.name == "whisper"
        self._queue: queue.Queue = queue.Queue()
        self._pending: deque[Utterance] = deque()
        self._last_text = ""
        self._last_audio_at = time.monotonic()
        self._stop = threading.Event()
        self.busy = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="asr", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- public (any thread)
    def push(self, chunk: AudioChunk) -> None:
        self._queue.put(("audio", chunk))

    def reset(self) -> None:
        self._queue.put(("reset", None))

    def flush(self) -> None:
        self._queue.put(("flush", None))

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(("stop", None))

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def backlog(self) -> int:
        return self._queue.qsize() + len(self._pending)

    # ------------------------------------------------------------- worker thread
    def _handle(self, kind: str, payload) -> bool:
        if kind == "stop":
            return False
        if kind == "reset":
            self.segmenter.reset()
            self._pending.clear()
            self._last_text = ""
        elif kind == "flush":
            self._pending.extend(self.segmenter.flush())
        elif kind == "audio":
            self._last_audio_at = time.monotonic()
            self._pending.extend(self.segmenter.feed(payload))
        return True

    def _drain(self) -> bool:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return True
            if not self._handle(kind, payload):
                return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._pending:
                try:
                    kind, payload = self._queue.get(timeout=0.25)
                except queue.Empty:
                    # Stream stalled mid-phrase: don't sit on the words forever.
                    if time.monotonic() - self._last_audio_at > 1.5:
                        self._pending.extend(self.segmenter.flush())
                        self._last_audio_at = time.monotonic()
                    continue
                if not self._handle(kind, payload):
                    return
            if not self._drain():
                return
            if self.on_level:
                self.on_level(self.segmenter.last_prob)
            if not self._pending:
                continue
            utt, merged = self._take()
            try:
                self._transcribe(utt, merged)
            except Exception:  # noqa: BLE001
                log.exception("speech recognition failed")

    def _take(self) -> tuple[Utterance, int]:
        """Next phrase; when we're behind, merge adjacent phrases into one call to catch up."""
        first = self._pending.popleft()
        if not self._pending:
            return first, 1
        parts = [first]
        while self._pending:
            nxt = self._pending[0]
            gap = nxt.start - parts[-1].end
            total = nxt.end - parts[0].start
            if gap > 1.0 or total > self.MAX_MERGED_SECONDS:
                break
            parts.append(self._pending.popleft())
        if len(parts) == 1:
            return first, 1
        audio = [parts[0].audio]
        for prev, cur in itertools.pairwise(parts):
            silence = max(0, int((cur.start - prev.end) * 16000))
            if silence:
                audio.append(np.zeros(min(silence, 16000), dtype=np.float32))
            audio.append(cur.audio)
        merged = Utterance(
            start=parts[0].start,
            end=parts[-1].end,
            audio=np.concatenate(audio),
            forced_cut=parts[-1].forced_cut,
            arrived_at=max(p.arrived_at for p in parts),
            closed_at=max(p.closed_at for p in parts),
            speech_ratio=float(np.mean([p.speech_ratio for p in parts])),
        )
        return merged, len(parts)

    def _transcribe(self, utt: Utterance, merged: int) -> None:
        self.busy = True
        try:
            prompt = self._last_text[-40:] if self.use_prompt and self._last_text else None
            result = self.engine.transcribe(utt.audio, prompt)
        finally:
            self.busy = False
        text = filters.clean(
            result.text,
            utt.duration,
            avg_logprob=result.avg_logprob,
            no_speech_prob=result.no_speech_prob,
            speech_ratio=utt.speech_ratio,
        )
        if prompt and text and text.strip() == prompt.strip():
            text = None  # echoed the prompt back
        if not text:
            self.dropped += 1
            log.debug("dropped ASR output %r", result.text)
            return
        self._last_text = text
        self.on_transcript(
            Transcript(
                id=next(_ids),
                start=utt.start,
                end=utt.end,
                text=text,
                forced_cut=utt.forced_cut,
                arrived_at=utt.arrived_at,
                closed_at=utt.closed_at,
                asr_done_at=time.time(),
                asr_seconds=result.elapsed,
                merged=merged,
            )
        )
