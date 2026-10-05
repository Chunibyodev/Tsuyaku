"""Streaming voice activity detection and phrase segmentation.

Silero VAD (ONNX, CPU, ~0.1 ms per 32 ms frame) scores every frame. A phrase ends after a
short silence, or - for streamers who never pause - is force-cut at the quietest frame
before `max_segment_s`, so subtitles never wait for a breath.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import VADConfig
from ..ingest.audio import SAMPLE_RATE, AudioChunk

log = logging.getLogger(__name__)

FRAME = 512  # 32 ms @ 16 kHz
CONTEXT = 64
FRAME_S = FRAME / SAMPLE_RATE


def default_model_path() -> Path:
    import faster_whisper

    assets = Path(faster_whisper.__file__).parent / "assets"
    candidates = sorted(assets.glob("silero_vad*.onnx"))
    if not candidates:
        raise FileNotFoundError("Silero VAD model not found in faster_whisper assets")
    return candidates[-1]


class SileroVAD:
    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(
            str(path or default_model_path()), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame]).reshape(1, -1).astype(np.float32, copy=False)
        out, self._h, self._c = self.session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = frame[-CONTEXT:].copy()
        return float(out.reshape(-1)[0])


@dataclass
class Utterance:
    start: float  # media time
    end: float
    audio: np.ndarray
    forced_cut: bool = False
    arrived_at: float = 0.0  # wall time the newest audio in this phrase reached us
    closed_at: float = 0.0  # wall time the segmenter closed the phrase
    speech_ratio: float = 1.0

    @property
    def duration(self) -> float:
        return len(self.audio) / SAMPLE_RATE


@dataclass
class _Frame:
    t: float
    samples: np.ndarray
    prob: float
    arrived_at: float


@dataclass
class _State:
    frames: list[_Frame] = field(default_factory=list)
    silence: float = 0.0


class Segmenter:
    def __init__(self, cfg: VADConfig, vad: SileroVAD | None = None) -> None:
        self.cfg = cfg
        self.vad = vad or SileroVAD()
        self._pending = np.zeros(0, dtype=np.float32)
        self._pending_t: float | None = None
        self._arrived_at = 0.0
        self._preroll: list[_Frame] = []
        self._speech: _State | None = None
        self.last_prob = 0.0

    @property
    def neg_threshold(self) -> float:
        return max(0.05, self.cfg.threshold - 0.15)

    def reset(self) -> None:
        self.vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self._pending_t = None
        self._preroll = []
        self._speech = None

    def feed(self, chunk: AudioChunk) -> list[Utterance]:
        out: list[Utterance] = []
        expected = None
        if self._pending_t is not None:
            expected = self._pending_t + len(self._pending) / SAMPLE_RATE
        if expected is not None and abs(chunk.start - expected) > 0.15:
            # Discontinuity (missed segment, seek): close what we have and restart.
            out.extend(self.flush())
            self._pending_t = chunk.start
        elif self._pending_t is None:
            self._pending_t = chunk.start
        self._arrived_at = max(self._arrived_at, chunk.arrived_at)
        self._pending = np.concatenate([self._pending, chunk.samples])

        n_frames = len(self._pending) // FRAME
        for i in range(n_frames):
            samples = self._pending[i * FRAME:(i + 1) * FRAME]
            t = self._pending_t + i * FRAME_S
            prob = self.vad(samples)
            self.last_prob = prob
            out.extend(self._step(_Frame(t, samples, prob, self._arrived_at)))
        if n_frames:
            self._pending = self._pending[n_frames * FRAME:].copy()
            self._pending_t += n_frames * FRAME_S
        return out

    def flush(self) -> list[Utterance]:
        out: list[Utterance] = []
        if self._speech and self._speech.frames:
            utt = self._make(self._speech.frames, forced=False)
            if utt:
                out.append(utt)
        self._speech = None
        self._preroll = []
        self.vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self._pending_t = None
        return out

    # ------------------------------------------------------------------ internals
    def _step(self, frame: _Frame) -> list[Utterance]:
        cfg = self.cfg
        pad_frames = max(0, int(cfg.pad_ms / 1000 / FRAME_S))
        if self._speech is None:
            if frame.prob >= cfg.threshold:
                self._speech = _State(frames=self._preroll[-pad_frames:] + [frame] if pad_frames else [frame])
                self._preroll = []
            else:
                self._preroll.append(frame)
                if len(self._preroll) > max(pad_frames, 1) * 2:
                    self._preroll = self._preroll[-max(pad_frames, 1):]
            return []

        st = self._speech
        st.frames.append(frame)
        if frame.prob < self.neg_threshold:
            st.silence += FRAME_S
        elif frame.prob >= cfg.threshold:
            st.silence = 0.0
        out: list[Utterance] = []

        if st.silence * 1000 >= cfg.min_silence_ms:
            silent_frames = int(round(st.silence / FRAME_S))
            keep = len(st.frames) - silent_frames + pad_frames
            phrase, rest = st.frames[:keep], st.frames[keep:]
            utt = self._make(phrase, forced=False)
            if utt:
                out.append(utt)
            self._speech = None
            self._preroll = rest[-pad_frames:] if pad_frames else []
            return out

        duration = len(st.frames) * FRAME_S
        if duration >= cfg.max_segment_s:
            cut = self._quiet_cut(st.frames)
            phrase, rest = st.frames[:cut], st.frames[cut:]
            utt = self._make(phrase, forced=True)
            if utt:
                out.append(utt)
            self._speech = _State(frames=rest, silence=0.0)
        return out

    def _quiet_cut(self, frames: list[_Frame]) -> int:
        """Index to cut at: the least speech-like frame in the back part of the phrase."""
        n = len(frames)
        lo = max(1, int(min(1.0, self.cfg.max_segment_s * 0.4) / FRAME_S))
        hi = max(lo + 1, n - 2)
        window = frames[lo:hi]
        if not window:
            return n
        # Prefer low VAD probability, then low energy.
        def score(f: _Frame) -> float:
            rms = float(np.sqrt(np.mean(f.samples**2)) + 1e-9)
            return f.prob + rms * 2.0
        best = min(range(len(window)), key=lambda i: score(window[i]))
        return lo + best + 1

    def _make(self, frames: list[_Frame], forced: bool) -> Utterance | None:
        if not frames:
            return None
        speech = sum(1 for f in frames if f.prob >= self.cfg.threshold)
        if speech * FRAME_S < self.cfg.min_segment_s:
            return None
        audio = np.concatenate([f.samples for f in frames])
        return Utterance(
            start=frames[0].t,
            end=frames[-1].t + FRAME_S,
            audio=audio,
            forced_cut=forced,
            arrived_at=max(f.arrived_at for f in frames),
            closed_at=time.time(),
            speech_ratio=speech / len(frames),
        )
