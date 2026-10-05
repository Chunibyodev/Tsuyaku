"""Live ingest: follow the audio playlist from the newest segment and feed the audio decoder."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from .audio import AudioChunk, SegmentDecoderThread
from .hls import HLSTrackFetcher, Segment
from .resolver import StreamInfo

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class IngestStats:
    segment_duration: float = 0.0
    youtube_latency: float | None = None  # source -> us, from EXT-X-PROGRAM-DATE-TIME
    listing_delay: float = 0.0  # playlist listed -> downloaded
    last_arrival: float = 0.0
    segments: int = 0
    gaps: int = 0
    live_edge_media_time: float | None = None  # media time of the newest audio sample


class LiveIngest:
    def __init__(
        self,
        info: StreamInfo,
        on_audio: Callable[[AudioChunk], None],
        *,
        poll_interval: float = 0.25,
        edge_segments: int = 1,
        refresh: Callable[[], Awaitable[StreamInfo]] | None = None,
        proxy: str | None = None,
    ) -> None:
        self.info = info
        self.on_audio = on_audio
        self.poll_interval = poll_interval
        self.edge_segments = edge_segments
        self.refresh = refresh
        self.stats = IngestStats()
        self._decoder = SegmentDecoderThread(self._on_chunk)
        self._fetchers: list[HLSTrackFetcher] = []
        self._client = httpx.AsyncClient(
            http2=False,
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, read=15.0),
            headers={"User-Agent": info.http_headers.get("User-Agent", USER_AGENT)},
            proxy=proxy,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        self.ended = asyncio.Event()

    def _on_chunk(self, chunk: AudioChunk) -> None:
        self.stats.live_edge_media_time = chunk.end
        self.on_audio(chunk)

    # The fetcher calls this on the event loop thread; everything here is non-blocking.
    def _on_segment(self, seg: Segment) -> None:
        if seg.is_init:
            self._decoder.push_init(seg.data)
            return
        self._decoder.push_segment(seg.data, seg.fetched_at)
        st = self.stats
        st.segments += 1
        st.segment_duration = seg.duration or st.segment_duration
        st.last_arrival = seg.fetched_at
        st.listing_delay = max(0.0, seg.fetched_at - seg.listed_at)
        if seg.youtube_latency is not None:
            st.youtube_latency = seg.youtube_latency

    async def _refresh_url(self, kind: str) -> str:
        if not self.refresh:
            raise RuntimeError("stream URL expired and cannot be refreshed")
        info = await self.refresh()
        track = info.audio if kind in ("audio", "muxed") else info.video
        if track is None:
            raise RuntimeError("refreshed stream has no matching track")
        return track.url

    async def run(self) -> None:
        info = self.info
        tracks = [("muxed" if info.audio.kind == "muxed" else "audio", info.audio.url)]
        for kind, url in tracks:
            self._fetchers.append(
                HLSTrackFetcher(
                    kind,
                    url,
                    self._client,
                    self._on_segment,
                    poll_interval=self.poll_interval,
                    refresh_url=lambda k=kind: self._refresh_url(k),
                )
            )
        try:
            playlists = await asyncio.gather(*(f.fetch_playlist() for f in self._fetchers))
            start = min(p.last_seq for p in playlists) - max(1, self.edge_segments) + 1
            start = max(start, max(p.media_sequence for p in playlists))
            self.stats.segment_duration = playlists[0].target_duration
            log.info("live ingest: %d track(s), target duration %.1fs, starting at seq %d",
                     len(tracks), playlists[0].target_duration, start)
            await asyncio.gather(*(f.run(start_seq=start) for f in self._fetchers))
        finally:
            self.ended.set()
            self.stats.gaps = sum(f.gaps for f in self._fetchers)

    async def close(self) -> None:
        for f in self._fetchers:
            f.stop()
        self._decoder.stop()
        await self._client.aclose()

    def gaps(self) -> int:
        return sum(f.gaps for f in self._fetchers)

    def seconds_since_last_segment(self) -> float:
        return time.time() - self.stats.last_arrival if self.stats.last_arrival else 0.0
