

from tsuyaku.config import Config
from tsuyaku.ingest.resolver import _classify, _pick_tracks, normalize_url, video_id_from_url


def test_config_roundtrip(tmp_path):
    path = tmp_path / "c.toml"
    cfg = Config()
    cfg.subtitles.context_lines = 3
    cfg.llm.extra_args = ["-fa", "on"]
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.subtitles.context_lines == 3
    assert loaded.llm.extra_args == ["-fa", "on"]
    # Partial files, unknown keys and loosely typed values are tolerated.
    path.write_text('[unknown]\nx = 1\n[asr]\nwindow_s = "8"\nbogus = 3\n[vad]\nthreshold = "oops"\n')
    loaded = Config.load(path)
    assert loaded.asr.window_s == 8
    assert loaded.vad.threshold == 0.5
    assert loaded.subtitles.context_lines == 6


def test_pick_tracks_split_hls():
    formats = [
        {"format_id": "233", "protocol": "m3u8_native", "url": "a1", "vcodec": "none", "acodec": None,
         "resolution": "audio only"},
        {"format_id": "234", "protocol": "m3u8_native", "url": "a2", "vcodec": "none", "acodec": None, "tbr": 128},
        {"format_id": "232", "protocol": "m3u8_native", "url": "v720", "vcodec": "avc1.4D401F", "acodec": "none",
         "height": 720},
        {"format_id": "270", "protocol": "m3u8_native", "url": "v1080", "vcodec": "avc1.4D4028", "acodec": "none",
         "height": 1080},
    ]
    video, audio = _pick_tracks(formats, 720, live=True)
    assert video.url == "v720" and video.kind == "video"
    assert audio.url == "a2" and audio.kind == "audio"


def test_pick_tracks_muxed():
    formats = [
        {"format_id": "95", "protocol": "m3u8_native", "url": "m720", "vcodec": "avc1", "acodec": "mp4a.40.2",
         "height": 720},
        {"format_id": "96", "protocol": "m3u8_native", "url": "m1080", "vcodec": "avc1", "acodec": "mp4a.40.2",
         "height": 1080},
    ]
    video, audio = _pick_tracks(formats, 1080, live=True)
    assert video is audio and video.url == "m1080" and video.kind == "muxed"


def test_url_helpers():
    assert video_id_from_url("https://www.youtube.com/watch?v=iAqxbx1q30c&t=1") == "iAqxbx1q30c"
    assert video_id_from_url("https://youtu.be/iAqxbx1q30c") == "iAqxbx1q30c"
    assert video_id_from_url("https://www.youtube.com/live/iAqxbx1q30c") == "iAqxbx1q30c"
    assert normalize_url("@weathernews") == "https://www.youtube.com/@weathernews/live"
    assert normalize_url("iAqxbx1q30c") == "https://www.youtube.com/watch?v=iAqxbx1q30c"
    assert _classify("Sign in to confirm you’re not a bot") == "bot_check"
    assert _classify("Join this channel to get access to members-only content") == "members_only"


def test_unknown_settings_in_config_are_ignored(tmp_path):
    """Settings Tsuyaku doesn't know (from older or newer versions) are dropped, the rest load."""
    path = tmp_path / "old.toml"
    path.write_text('[ui]\nweb_theme = "light"\n[chat]\nview = "tsuyaku"\ntranslate = false\n'
                    '[subtitles]\nsync_delay_s = 2.0\ncontext_lines = 4\n[compose]\ngroq_key = "k"\n')
    cfg = Config.load(path)
    assert cfg.chat.translate is False and cfg.subtitles.context_lines == 4
    assert cfg.compose.cloud_key("groq") == "k" and cfg.compose.provider == "local"
    assert "ui" not in cfg.to_dict()


def test_config_migrates_old_llm_defaults(tmp_path):
    path = tmp_path / "old.toml"
    path.write_text("[llm]\nctx_size = 16384\nparallel = 4\n")
    cfg = Config.load(path)
    assert cfg.llm.ctx_size == 8192 and cfg.llm.parallel == 3 and cfg.meta.version == 2
    path.write_text("[llm]\nctx_size = 16384\n[meta]\nversion = 2\n")
    assert Config.load(path).llm.ctx_size == 16384  # explicit choice after migration is kept
