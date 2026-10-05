"""Live-edge HLS fetching.

YouTube serves live streams as HLS media playlists (fMP4 or MPEG-TS segments, often with
separate audio and video tracks). Generic players start a few segments behind the newest
one and reload the playlist lazily. For the lowest latency we:

* start at the newest segment,
* schedule the next playlist poll for just before the next segment is due, then poll
  quickly until it shows up,
* download segments the moment they are listed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)


@dataclass
class PlaylistSegment:
    seq: int
    uri: str
    duration: float
    program_date_time: datetime | None = None
    discontinuity: bool = False


@dataclass
class MediaPlaylist:
    target_duration: float
    media_sequence: int
    segments: list[PlaylistSegment] = field(default_factory=list)
    init_uri: str | None = None
    ended: bool = False

    @property
    def last_seq(self) -> int:
        return self.segments[-1].seq if self.segments else self.media_sequence - 1


def _parse_attrs(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    key, value, in_quotes, buf = None, None, False, ""
    for ch in text + ",":
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "=" and not in_quotes and key is None:
            key, buf = buf.strip(), ""
            continue
        if ch == "," and not in_quotes:
            if key is not None:
                value = buf
                attrs[key] = value
            key, buf = None, ""
            continue
        buf += ch
    return attrs


def _parse_pdt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("Not an HLS playlist")
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        raise ValueError("Got a master playlist, expected a media playlist")

    target = 5.0
    media_seq = 0
    init_uri = None
    ended = False
    segments: list[PlaylistSegment] = []
    pending_duration: float | None = None
    pending_pdt: datetime | None = None
    pending_disc = False
    seq = None
    for ln in lines[1:]:
        if ln.startswith("#EXT-X-TARGETDURATION:"):
            target = float(ln.split(":", 1)[1])
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_seq = int(ln.split(":", 1)[1])
        elif ln.startswith("#EXT-X-MAP:"):
            uri = _parse_attrs(ln.split(":", 1)[1]).get("URI")
            if uri:
                init_uri = urljoin(base_url, uri)
        elif ln.startswith("#EXTINF:"):
            pending_duration = float(ln.split(":", 1)[1].split(",", 1)[0])
        elif ln.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = _parse_pdt(ln.split(":", 1)[1])
        elif ln.startswith("#EXT-X-DISCONTINUITY") and not ln.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
            pending_disc = True
        elif ln.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif not ln.startswith("#"):
            if seq is None:
                seq = media_seq
            segments.append(
                PlaylistSegment(
                    seq=seq,
                    uri=urljoin(base_url, ln),
                    duration=pending_duration if pending_duration is not None else target,
                    program_date_time=pending_pdt,
                    discontinuity=pending_disc,
                )
            )
            seq += 1
            pending_duration, pending_pdt, pending_disc = None, None, False
    return MediaPlaylist(target, media_seq, segments, init_uri, ended)


@dataclass
class Segment:
    """A downloaded media segment (or init segment) of one track."""

    track: str
    seq: int
    data: bytes
    duration: float = 0.0
    program_date_time: datetime | None = None
    fetched_at: float = 0.0  # time.time() when the download finished
    listed_at: float = 0.0  # time.time() when we first saw it in the playlist
    is_init: bool = False

    @property
    def youtube_latency(self) -> float | None:
        """Wall-clock delay between the end of this segment at the source and its arrival here."""
        if self.program_date_time is None:
            return None
        end = self.program_date_time.timestamp() + self.duration
        return self.fetched_at - end


class PlaylistExpired(Exception):
    pass


SegmentCallback = Callable[[Segment], None]
RefreshCallback = Callable[[], Awaitable[str]]


class HLSTrackFetcher:
    """Follows one live media playlist and delivers segments in order."""

    def __init__(
        self,
        track: str,
        playlist_url: str,
        client: httpx.AsyncClient,
        on_segment: SegmentCallback,
        *,
        poll_interval: float = 0.25,
        refresh_url: RefreshCallback | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.track = track
        self.playlist_url = playlist_url
        self.client = client
        self.on_segment = on_segment
        self.poll_interval = max(0.1, poll_interval)
        self.refresh_url = refresh_url
        self.headers = headers or {}
        self.next_seq: int | None = None
        self.init_sent_for: str | None = None
        self.last_playlist: MediaPlaylist | None = None
        self.segments_received = 0
        self.gaps = 0
        self._listed_at: dict[int, float] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def fetch_playlist(self) -> MediaPlaylist:
        resp = await self.client.get(self.playlist_url, headers=self.headers)
        if resp.status_code in (403, 410):
            raise PlaylistExpired(f"{self.track} playlist returned {resp.status_code}")
        resp.raise_for_status()
        playlist = parse_media_playlist(resp.text, str(resp.url))
        now = time.time()
        for seg in playlist.segments:
            self._listed_at.setdefault(seg.seq, now)
        if len(self._listed_at) > 256:
            for key in sorted(self._listed_at)[:-128]:
                del self._listed_at[key]
        self.last_playlist = playlist
        return playlist

    async def _get(self, url: str, attempts: int = 3) -> bytes:
        for attempt in range(attempts):
            try:
                resp = await self.client.get(url, headers=self.headers)
                if resp.status_code in (403, 410):
                    raise PlaylistExpired(f"{self.track} segment returned {resp.status_code}")
                resp.raise_for_status()
                return resp.content
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == attempts - 1:
                    raise
                log.debug("%s: retrying %s (%s)", self.track, url[:80], exc)
                await asyncio.sleep(0.2 * (attempt + 1))
        raise RuntimeError("unreachable")

    async def _send_init(self, playlist: MediaPlaylist) -> None:
        if playlist.init_uri and playlist.init_uri != self.init_sent_for:
            data = await self._get(playlist.init_uri)
            self.init_sent_for = playlist.init_uri
            self.on_segment(Segment(self.track, -1, data, fetched_at=time.time(), is_init=True))

    async def run(self, start_seq: int | None = None, edge_segments: int = 1) -> None:
        """Fetch until the stream ends or stop() is called."""
        self.next_seq = start_seq
        expected_next_at: float | None = None
        while not self._stop.is_set():
            try:
                playlist = await self.fetch_playlist()
            except PlaylistExpired:
                if not self.refresh_url:
                    raise
                log.info("%s: playlist expired, refreshing URL", self.track)
                self.playlist_url = await self.refresh_url()
                self.init_sent_for = None
                continue
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                log.warning("%s: playlist fetch failed: %s", self.track, exc)
                await self._sleep(1.0)
                continue

            await self._send_init(playlist)

            if self.next_seq is None:
                self.next_seq = max(playlist.media_sequence, playlist.last_seq - max(1, edge_segments) + 1)
            if self.next_seq < playlist.media_sequence:
                self.gaps += 1
                log.warning("%s: fell behind (wanted %d, oldest %d); jumping ahead",
                            self.track, self.next_seq, playlist.media_sequence)
                self.next_seq = playlist.media_sequence

            new = [s for s in playlist.segments if s.seq >= self.next_seq]
            if new:
                await self._download_in_order(new)
                last = new[-1]
                self.next_seq = last.seq + 1
                expected_next_at = time.monotonic() + last.duration
            if playlist.ended and self.next_seq > playlist.last_seq:
                log.info("%s: stream ended", self.track)
                return

            # Sleep until shortly before the next segment is due, then poll quickly.
            delay = self.poll_interval
            if new and expected_next_at is not None:
                delay = max(self.poll_interval, expected_next_at - time.monotonic() - 0.35)
            await self._sleep(delay)

    async def _download_in_order(self, segments: list[PlaylistSegment]) -> None:
        tasks = [asyncio.create_task(self._get(s.uri)) for s in segments]
        try:
            for seg, task in zip(segments, tasks, strict=True):
                try:
                    data = await task
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    log.warning("%s: segment %d failed: %s", self.track, seg.seq, exc)
                    self.gaps += 1
                    continue
                self.segments_received += 1
                self.on_segment(
                    Segment(
                        self.track,
                        seg.seq,
                        data,
                        duration=seg.duration,
                        program_date_time=seg.program_date_time,
                        fetched_at=time.time(),
                        listed_at=self._listed_at.get(seg.seq, time.time()),
                    )
                )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
