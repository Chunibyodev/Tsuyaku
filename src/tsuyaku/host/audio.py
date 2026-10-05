"""Audio the Firefox extension captures from YouTube's player, made ready for the phrase splitter.

The page sends 16-bit mono PCM at the browser's sample rate together with the video time of the
first sample and the player's volume (already applied to the sound, so it is undone here). The VAD needs an unbroken clock, but the video's clock can run at another speed
(1.25x, YouTube catching up to live) or jump (seek, ad, "jump to live"). So chunks are placed on
a clock of their own, with a gap at every jump (which closes the phrase being spoken), and each
chunk remembers where it sits in the video. Recognised phrases are mapped back to video time.
"""

from __future__ import annotations

import bisect
from collections import deque

import av
import numpy as np

from ..ingest.audio import SAMPLE_RATE, AudioChunk

# Clock gap inserted at a discontinuity: well over the VAD's 0.15 s "jump" threshold.
GAP_S = 1.0
# Firefox applies the video's volume before the page can tap its sound, so quiet playback is
# turned back up (to at most this factor; below ~10% volume the VAD just gets quieter audio).
MAX_VOLUME_GAIN = 10.0


class PageAudio:
    def __init__(self, keep_s: float = 600.0) -> None:
        self.clock = 0.0
        self.keep_s = keep_s
        # (clock time, video time, playback speed) at the start of every chunk
        self._clock: deque[float] = deque()
        self._anchors: deque[tuple[float, float]] = deque()
        self._rate = 0
        self._resampler: av.AudioResampler | None = None
        self.chunks = 0

    def push(self, pcm: bytes, rate: int, video_time: float, speed: float = 1.0,
             continuous: bool = True, arrived_at: float = 0.0, volume: float = 1.0) -> AudioChunk | None:
        """One captured buffer -> a 16 kHz chunk on our clock (None if nothing came out yet)."""
        if rate <= 0:
            return None
        if not continuous or rate != self._rate:
            self._resampler = None
            if self.chunks:
                self.clock += GAP_S
        self._rate = rate
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if 0.0 < volume < 1.0:
            samples = np.clip(samples * min(MAX_VOLUME_GAIN, 1.0 / volume), -1.0, 1.0)
        out = self._resample(samples, rate)
        if not out.size:
            return None
        start = self.clock
        self._clock.append(start)
        self._anchors.append((float(video_time), float(speed) if speed > 0 else 1.0))
        while self._clock and start - self._clock[0] > self.keep_s:
            self._clock.popleft()
            self._anchors.popleft()
        self.clock += out.size / SAMPLE_RATE
        self.chunks += 1
        return AudioChunk(start, out, arrived_at)

    def _resample(self, samples: np.ndarray, rate: int) -> np.ndarray:
        if rate == SAMPLE_RATE:
            return samples
        if self._resampler is None:
            self._resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = rate
        frame.pts = None
        frames = self._resampler.resample(frame)
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([f.to_ndarray().reshape(-1) for f in frames]).astype(np.float32, copy=False)

    def to_video(self, clock_time: float) -> float:
        """Video time for a point on our clock."""
        if not self._clock:
            return clock_time
        i = bisect.bisect_right(self._clock, clock_time) - 1
        i = max(0, i)
        start = self._clock[i]
        video, speed = self._anchors[i]
        return video + (clock_time - start) * speed
