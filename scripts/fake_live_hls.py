"""Serve a local media file as a fake *live* HLS stream (split fMP4 audio/video tracks,
like YouTube), for testing Tsuyaku without YouTube.

    python scripts/fake_live_hls.py input.mp4 --port 8899
    tsuyaku run "hls-split:http://127.0.0.1:8899/video.m3u8|http://127.0.0.1:8899/audio.m3u8"

Segments become visible in the playlists in real time (one per --segment seconds).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def segment(src: Path, out: Path, seconds: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    common = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src)]
    gop = str(int(30 * seconds))
    subprocess.run(
        common + ["-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-g", gop, "-keyint_min", gop,
                  "-sc_threshold", "0", "-f", "hls", "-hls_time", str(seconds), "-hls_playlist_type", "vod",
                  "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "v_init.mp4",
                  "-hls_segment_filename", str(out / "v_%05d.m4s"), str(out / "v.m3u8")],
        check=True,
    )
    subprocess.run(
        common + ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-f", "hls", "-hls_time", str(seconds),
                  "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "a_init.mp4",
                  "-hls_segment_filename", str(out / "a_%05d.m4s"), str(out / "a.m3u8")],
        check=True,
    )


def durations(playlist: Path) -> list[float]:
    return [float(ln.split(":")[1].split(",")[0]) for ln in playlist.read_text().splitlines()
            if ln.startswith("#EXTINF:")]


class State:
    def __init__(self, folder: Path, seg_seconds: float, window: int) -> None:
        self.folder = folder
        self.seg = seg_seconds
        self.window = window
        self.start = time.time()
        self.start_wall = datetime.now(UTC)
        self.vd = durations(folder / "v.m3u8")
        self.ad = durations(folder / "a.m3u8")
        self.count = min(len(self.vd), len(self.ad))

    def available(self) -> int:
        return min(self.count, int((time.time() - self.start) / self.seg))

    def playlist(self, kind: str) -> str:
        n = self.available()
        first = max(0, n - self.window)
        durs = self.vd if kind == "v" else self.ad
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{int(round(self.seg))}",
                 f"#EXT-X-MEDIA-SEQUENCE:{first}", f'#EXT-X-MAP:URI="{kind}_init.mp4"']
        for i in range(first, n):
            pdt = self.start_wall + timedelta(seconds=sum(durs[:i]))
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt.isoformat(timespec='milliseconds')}")
            lines.append(f"#EXTINF:{durs[i]:.3f},")
            lines.append(f"{kind}_{i:05d}.m4s")
        if n >= self.count:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            name = self.path.split("?")[0].lstrip("/")
            if name in ("video.m3u8", "audio.m3u8"):
                body = state.playlist("v" if name == "video.m3u8" else "a").encode()
                ctype = "application/vnd.apple.mpegurl"
            else:
                path = state.folder / name
                if not path.exists() or "/" in name:
                    self.send_error(404)
                    return
                body = path.read_bytes()
                ctype = "video/mp4"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(src: Path, port: int, seg_seconds: float = 1.0, window: int = 6) -> tuple[ThreadingHTTPServer, Path]:
    folder = Path(tempfile.mkdtemp(prefix="fakehls_"))
    segment(src, folder, seg_seconds)
    state = State(folder, seg_seconds, window)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, folder


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--segment", type=float, default=1.0)
    args = ap.parse_args()
    server, folder = serve(Path(args.input), args.port, args.segment)
    base = f"http://127.0.0.1:{args.port}"
    print(f"Serving fake live stream:\n  hls-split:{base}/video.m3u8|{base}/audio.m3u8")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
