"""Turn a YouTube URL into playable track URLs using yt-dlp."""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..cookies import CookieSource

log = logging.getLogger(__name__)

_HLS_PROTOCOLS = {"m3u8", "m3u8_native"}


class ResolveError(Exception):
    """yt-dlp could not give us a stream. `kind` lets the UI show a helpful hint."""

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Track:
    kind: str  # "audio", "video" or "muxed"
    url: str
    format_id: str
    protocol: str
    height: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    bitrate: float | None = None

    @property
    def is_hls(self) -> bool:
        return self.protocol in _HLS_PROTOCOLS


@dataclass
class StreamInfo:
    video_id: str
    webpage_url: str
    title: str
    channel: str
    channel_id: str
    description: str
    live_status: str  # is_live, was_live, not_live, is_upcoming, post_live
    video: Track | None
    audio: Track
    duration: float | None = None
    thumbnail: str | None = None
    http_headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.live_status == "is_live"

    @property
    def split(self) -> bool:
        return self.video is not None and self.video is not self.audio and self.video.kind == "video"


def video_id_from_url(url: str) -> str | None:
    patterns = [
        r"(?:v=|/live/|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        m = re.search(pattern, url.strip())
        if m:
            return m.group(1)
    return None


def normalize_url(url: str) -> str:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return f"https://www.youtube.com/watch?v={url}"
    if url.startswith("@"):
        return f"https://www.youtube.com/{url}/live"
    return url


def js_runtimes() -> dict[str, dict]:
    """JavaScript runtimes for yt-dlp's YouTube challenge solver (bundled Deno first)."""
    runtimes: dict[str, dict] = {}
    bundled = paths.bin_dir() / "deno" / paths.exe_name("deno")
    if bundled.exists():
        runtimes["deno"] = {"path": str(bundled)}
    elif shutil.which("deno"):
        runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    if shutil.which("bun"):
        runtimes["bun"] = {}
    return runtimes or {"deno": {}}


def ytdlp_params(cookies: CookieSource | None = None, quiet: bool = True) -> dict:
    params: dict = {
        "quiet": quiet,
        "no_warnings": quiet,
        "skip_download": True,
        "noplaylist": True,
        "js_runtimes": js_runtimes(),
        "socket_timeout": 20,
    }
    if cookies:
        params.update(cookies.ytdlp_params())
    return params


def _codec(value: str | None) -> str | None:
    return None if value in (None, "none") else value


def _has_stream(f: dict, codec_key: str, ext_key: str) -> bool | None:
    """yt-dlp uses 'none' for "definitely absent" and None for "unknown"."""
    codec = f.get(codec_key)
    if codec == "none":
        return False
    if codec:
        return True
    if f.get(ext_key) == "none":
        return False
    return None


def _stream_kinds(f: dict) -> tuple[bool, bool]:
    has_video = _has_stream(f, "vcodec", "video_ext")
    has_audio = _has_stream(f, "acodec", "audio_ext")
    if has_video is None:
        has_video = bool(f.get("height") or f.get("width")) and f.get("resolution") != "audio only"
    if has_audio is None:
        # Unknown audio codec: an "audio only" format obviously has audio; a video format with
        # an unknown audio codec is almost always muxed.
        has_audio = True
    return has_video, has_audio


def _pick_tracks(formats: list[dict], max_height: int, live: bool) -> tuple[Track | None, Track]:
    def ok_protocol(f: dict) -> bool:
        proto = f.get("protocol") or ""
        return proto in _HLS_PROTOCOLS if live else proto in ("https", "http")

    usable = [f for f in formats if ok_protocol(f) and f.get("url")]
    kinds = {id(f): _stream_kinds(f) for f in usable}
    audio_only = [f for f in usable if kinds[id(f)] == (False, True)]
    video_only = [f for f in usable if kinds[id(f)] == (True, False)]
    muxed = [f for f in usable if kinds[id(f)] == (True, True)]

    def video_score(f: dict) -> tuple:
        height = f.get("height") or 0
        fits = height <= max_height
        vcodec = (f.get("vcodec") or "").lower()
        # H.264 decodes everywhere (incl. hardware on a GTX 1080 Ti); prefer it over VP9/AV1.
        codec_rank = 2 if vcodec.startswith("avc") else 1 if vcodec.startswith("vp") else 0
        return (fits, height if fits else -height, codec_rank, f.get("fps") or 0, f.get("tbr") or 0)

    def audio_score(f: dict) -> tuple:
        acodec = (f.get("acodec") or "").lower()
        return (acodec.startswith("mp4a"), f.get("abr") or f.get("tbr") or 0)

    def track(kind: str, f: dict) -> Track:
        return Track(
            kind=kind,
            url=f["url"],
            format_id=str(f.get("format_id")),
            protocol=f.get("protocol") or "",
            height=f.get("height"),
            fps=f.get("fps"),
            vcodec=_codec(f.get("vcodec")),
            acodec=_codec(f.get("acodec")),
            bitrate=f.get("tbr"),
        )

    if audio_only and video_only:
        return track("video", max(video_only, key=video_score)), track("audio", max(audio_only, key=audio_score))
    if muxed:
        best = track("muxed", max(muxed, key=video_score))
        return best, best
    if audio_only:
        return None, track("audio", max(audio_only, key=audio_score))
    raise ResolveError("No playable stream formats were found.", "formats")


def _classify(message: str) -> str:
    low = message.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return "bot_check"
    if "members" in low and ("only" in low or "join" in low):
        return "members_only"
    if "will begin" in low or "premieres" in low or "is_upcoming" in low or "live event will" in low:
        return "upcoming"
    if "private video" in low:
        return "private"
    if "sign in to confirm your age" in low or "age-restricted" in low:
        return "login"
    if "cookie" in low and ("load" in low or "decrypt" in low or "could not copy" in low):
        return "cookies"
    if "429" in low or "too many requests" in low:
        return "rate_limited"
    return "other"


def resolve(url: str, *, max_height: int = 1080, cookies: CookieSource | None = None) -> StreamInfo:
    """Blocking: run yt-dlp and pick tracks. Call from a worker thread."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    url = normalize_url(url)
    try:
        with YoutubeDL(ytdlp_params(cookies)) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc))
        raise ResolveError(msg, _classify(msg)) from exc
    except Exception as exc:  # cookie extraction errors etc.
        msg = str(exc)
        raise ResolveError(msg, _classify(msg)) from exc

    if info is None:
        raise ResolveError("yt-dlp returned no information.")
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise ResolveError("That page has no video.")
        info = entries[0]

    live_status = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    if live_status == "is_upcoming":
        raise ResolveError("The stream has not started yet.", "upcoming")
    live = live_status == "is_live"
    video, audio = _pick_tracks(info.get("formats") or [], max_height, live)
    if not live and not (audio.protocol in ("https", "http") or audio.is_hls):
        raise ResolveError("Unsupported stream protocol.", "formats")

    return StreamInfo(
        video_id=info.get("id") or video_id_from_url(url) or "",
        webpage_url=info.get("webpage_url") or url,
        title=info.get("title") or info.get("fulltitle") or "",
        channel=info.get("channel") or info.get("uploader") or "",
        channel_id=info.get("channel_id") or "",
        description=(info.get("description") or "")[:2000],
        live_status=live_status,
        video=video,
        audio=audio,
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        http_headers=dict(info.get("http_headers") or {}),
    )


def deno_path() -> Path | None:
    bundled = paths.bin_dir() / "deno" / paths.exe_name("deno")
    if bundled.exists():
        return bundled
    found = shutil.which("deno")
    return Path(found) if found else None
