import asyncio

import httpx

from tsuyaku.ingest.hls import HLSTrackFetcher, parse_media_playlist

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-MAP:URI="init.mp4"
#EXT-X-PROGRAM-DATE-TIME:2026-10-04T08:00:00.000+00:00
#EXTINF:2.000,
seg/100.m4s
#EXTINF:2.000,
seg/101.m4s
#EXTINF:1.500,
https://cdn.example.com/abs/102.m4s
"""


def test_parse_media_playlist():
    pl = parse_media_playlist(PLAYLIST, "https://host/path/index.m3u8")
    assert pl.target_duration == 2
    assert pl.media_sequence == 100
    assert pl.init_uri == "https://host/path/init.mp4"
    assert [s.seq for s in pl.segments] == [100, 101, 102]
    assert pl.segments[0].uri == "https://host/path/seg/100.m4s"
    assert pl.segments[2].uri == "https://cdn.example.com/abs/102.m4s"
    assert pl.segments[2].duration == 1.5
    assert pl.segments[0].program_date_time is not None
    assert not pl.ended


def test_master_playlist_rejected():
    import pytest

    with pytest.raises(ValueError):
        parse_media_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n", "https://h/")


def _live_server():
    """A fake live playlist that gains one segment every time it is fetched twice."""
    state = {"fetches": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".m3u8"):
            state["fetches"] += 1
            last = 5 + state["fetches"] // 2
            first = max(0, last - 4)
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}", '#EXT-X-MAP:URI="init.mp4"']
            for i in range(first, last + 1):
                lines += ["#EXTINF:1.0,", f"s{i}.m4s"]
            if last >= 9:
                lines.append("#EXT-X-ENDLIST")
            return httpx.Response(200, text="\n".join(lines))
        if path.endswith("init.mp4"):
            return httpx.Response(200, content=b"INIT")
        return httpx.Response(200, content=path.rsplit("/", 1)[-1].encode())

    return handler


def test_fetcher_starts_at_live_edge_and_delivers_in_order():
    got = []

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(_live_server()))
        f = HLSTrackFetcher("audio", "https://h/a.m3u8", client, got.append, poll_interval=0.1)
        await asyncio.wait_for(f.run(edge_segments=1), 20)
        await client.aclose()

    asyncio.run(run())
    assert got[0].is_init and got[0].data == b"INIT"
    seqs = [s.seq for s in got if not s.is_init]
    assert seqs == sorted(seqs)
    assert seqs[0] >= 5  # started at the live edge, not at the oldest segment
    assert seqs[-1] == 9
    assert len(set(seqs)) == len(seqs)
    assert sum(1 for s in got if s.is_init) == 1
