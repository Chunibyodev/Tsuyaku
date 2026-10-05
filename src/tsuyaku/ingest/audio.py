"""Decode stream audio to 16 kHz mono float32 with media timestamps.

Timestamps are the stream's own presentation times (seconds), the same clock the video
player reports with `rebase-start-time=no`, so subtitles line up exactly with playback.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import av
import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


@dataclass
class AudioChunk:
    start: float  # media time (seconds) of the first sample
    samples: np.ndarray  # float32 mono @ 16 kHz
    arrived_at: float = 0.0  # time.time() when the source data arrived

    @property
    def duration(self) -> float:
        return len(self.samples) / SAMPLE_RATE

    @property
    def end(self) -> float:
        return self.start + self.duration


def _resampler() -> av.AudioResampler:
    return av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)


def _frame_to_array(frames) -> np.ndarray:
    arrays = [f.to_ndarray().reshape(-1) for f in frames]
    if not arrays:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(arrays).astype(np.float32, copy=False)


class SegmentAudioDecoder:
    """Decodes a sequence of HLS segments (fMP4 with init segment, or MPEG-TS).

    One decoder and resampler are kept across segments so there are no clicks or gaps at
    segment boundaries.
    """

    def __init__(self) -> None:
        self.init: bytes = b""
        self._decoder: av.CodecContext | None = None
        self._resampler = _resampler()
        self._expected: float | None = None

    def set_init(self, data: bytes) -> None:
        self.init = data
        self._decoder = None

    def reset(self) -> None:
        self._decoder = None
        self._resampler = _resampler()
        self._expected = None

    def decode(self, data: bytes, arrived_at: float = 0.0) -> list[AudioChunk]:
        buf = io.BytesIO(self.init + data)
        chunks: list[AudioChunk] = []
        with av.open(buf, mode="r") as container:
            if not container.streams.audio:
                return chunks
            stream = container.streams.audio[0]
            if self._decoder is None:
                self._decoder = self._make_decoder(stream)
            time_base = stream.time_base
            for packet in container.demux(stream):
                if packet.size == 0 or packet.pts is None:
                    continue
                pts = float(packet.pts * time_base)
                try:
                    frames = self._decoder.decode(packet)
                except av.error.InvalidDataError:
                    log.debug("bad audio packet at %.3f", pts)
                    continue
                for frame in frames:
                    chunk = self._resample(frame, pts, arrived_at)
                    if chunk is not None:
                        chunks.append(chunk)
                    pts += frame.samples / frame.sample_rate
        return chunks

    def _make_decoder(self, stream) -> av.CodecContext:
        src = stream.codec_context
        dec = av.CodecContext.create(src.name, "r")
        if src.extradata:
            dec.extradata = src.extradata
        if src.sample_rate:
            dec.sample_rate = src.sample_rate
        try:
            dec.layout = src.layout
        except Exception:  # noqa: BLE001 - older PyAV
            pass
        return dec

    def _resample(self, frame, pts: float, arrived_at: float) -> AudioChunk | None:
        # A jump in timestamps (missed segment / discontinuity) restarts the resampler so
        # buffered samples don't smear across the gap.
        if self._expected is not None and abs(pts - self._expected) > 0.25:
            self._resampler = _resampler()
        self._expected = pts + frame.samples / frame.sample_rate
        frame.pts = None
        samples = _frame_to_array(self._resampler.resample(frame))
        if samples.size == 0:
            return None
        return AudioChunk(pts, samples, arrived_at)


class SegmentDecoderThread:
    """Runs SegmentAudioDecoder off the event loop; pushes AudioChunks to a callback."""

    def __init__(self, on_chunk: Callable[[AudioChunk], None]) -> None:
        self.on_chunk = on_chunk
        self.decoder = SegmentAudioDecoder()
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._thread = threading.Thread(target=self._run, name="audio-decoder", daemon=True)
        self._thread.start()

    def push_init(self, data: bytes) -> None:
        self._queue.put(("init", data, 0.0))

    def push_segment(self, data: bytes, arrived_at: float) -> None:
        try:
            self._queue.put_nowait(("seg", data, arrived_at))
        except queue.Full:
            log.warning("audio decoder is falling behind; dropping a segment")

    def stop(self) -> None:
        self._queue.put(("stop", b"", 0.0))

    def _run(self) -> None:
        while True:
            kind, data, arrived_at = self._queue.get()
            if kind == "stop":
                return
            if kind == "init":
                self.decoder.set_init(data)
                continue
            try:
                for chunk in self.decoder.decode(data, arrived_at):
                    self.on_chunk(chunk)
            except Exception:  # noqa: BLE001 - one bad segment must not kill the stream
                log.exception("audio segment decode failed")
                self.decoder.reset()


class FileAudioReader:
    """Decodes a whole file or URL (VOD) as fast as possible, or in real time (replay)."""

    def __init__(
        self,
        source: str,
        on_chunk: Callable[[AudioChunk], None],
        *,
        realtime: bool = False,
        headers: dict[str, str] | None = None,
        on_done: Callable[[], None] | None = None,
        start_at: float = 0.0,
        max_ahead: Callable[[float], bool] | None = None,
    ) -> None:
        self.source = source
        self.on_chunk = on_chunk
        self.realtime = realtime
        self.headers = headers or {}
        self.on_done = on_done
        self.start_at = start_at
        # max_ahead(media_time) -> True while we should wait (e.g. too far ahead of playback)
        self.max_ahead = max_ahead
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="file-audio", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        options = {}
        if self.headers:
            options["headers"] = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        try:
            with av.open(self.source, mode="r", options=options) as container:
                stream = container.streams.audio[0]
                if self.start_at > 0:
                    container.seek(int(self.start_at / stream.time_base), stream=stream)
                resampler = _resampler()
                t0_wall = time.monotonic()
                t0_media: float | None = None
                for frame in container.decode(stream):
                    if self._stop.is_set():
                        return
                    if frame.pts is None:
                        continue
                    pts = float(frame.pts * stream.time_base)
                    if t0_media is None:
                        t0_media = pts
                    if self.realtime:
                        wait = (pts - t0_media) - (time.monotonic() - t0_wall)
                        if wait > 0:
                            time.sleep(wait)
                    while self.max_ahead and self.max_ahead(pts) and not self._stop.is_set():
                        time.sleep(0.2)
                    frame.pts = None
                    samples = _frame_to_array(resampler.resample(frame))
                    if samples.size:
                        self.on_chunk(AudioChunk(pts, samples, time.time()))
        except Exception:  # noqa: BLE001
            log.exception("audio reader failed for %s", self.source[:120])
        finally:
            if self.on_done:
                self.on_done()
