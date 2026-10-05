"""The Firefox extension's native host: message framing, page audio, and a whole session with a
fake speech model and a fake translation server."""

import asyncio
import base64
import io
import json
import shutil
import subprocess
import sys
import zipfile

import httpx
import numpy as np
import pytest

from tsuyaku.asr.engines import ASRResult
from tsuyaku.config import Config
from tsuyaku.engine import Engine
from tsuyaku.host import install
from tsuyaku.host.audio import GAP_S, PageAudio
from tsuyaku.host.protocol import MAX_TO_BROWSER, Writer, encode_message, read_message
from tsuyaku.host.service import Host
from tsuyaku.mt.client import LLMClient

# ---------------------------------------------------------------------------- framing


def test_messages_round_trip_and_eof():
    buf = io.BytesIO(encode_message({"a": "日本語"}) + encode_message({"b": [1, 2]}))
    assert read_message(buf) == {"a": "日本語"}
    assert read_message(buf) == {"b": [1, 2]}
    assert read_message(buf) is None
    cut = encode_message({"x": 1})[:-2]  # the browser went away mid-message
    assert read_message(io.BytesIO(cut)) is None


def test_writer_skips_messages_the_browser_would_reject():
    out = io.BytesIO()
    w = Writer(out)
    w.send({"big": "x" * (MAX_TO_BROWSER + 10)})
    w.send({"ok": True})
    assert read_message(io.BytesIO(out.getvalue())) == {"ok": True}


# ---------------------------------------------------------------------------- page audio


def pcm48(seconds, amp=0.3, freq=220):
    t = np.arange(int(seconds * 48000)) / 48000
    return (amp * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2").tobytes()


def test_page_audio_resamples_on_a_continuous_clock():
    pa = PageAudio()
    a = pa.push(pcm48(0.1), 48000, 100.0)
    b = pa.push(pcm48(0.1), 48000, 100.1)
    assert a.start == 0.0 and b.start == pytest.approx(a.end)
    assert abs(len(b.samples) - 1600) < 40  # 48 kHz -> 16 kHz
    assert pa.to_video(b.start + 0.05) == pytest.approx(100.15, abs=0.01)


def test_page_audio_gap_at_jumps_and_playback_speed():
    pa = PageAudio()
    a = pa.push(pcm48(0.2), 48000, 10.0)
    b = pa.push(pcm48(0.2), 48000, 500.0, speed=1.5, continuous=False)  # seek / jump to live
    assert b.start >= a.end + GAP_S  # the VAD sees a jump and closes the phrase
    # At 1.5x, 0.1 s of sound covers 0.15 s of video.
    assert pa.to_video(b.start + 0.1) == pytest.approx(500.15, abs=0.01)
    assert pa.to_video(a.start + 0.1) == pytest.approx(10.1, abs=0.01)


def test_page_audio_undoes_the_player_volume():
    full = PageAudio().push(pcm48(0.2), 48000, 0.0)
    quiet = PageAudio().push(pcm48(0.2, amp=0.3 * 0.25), 48000, 0.0, volume=0.25)  # YouTube at 25%
    assert np.max(np.abs(quiet.samples)) == pytest.approx(np.max(np.abs(full.samples)), rel=0.02)
    tiny = PageAudio().push(pcm48(0.2, amp=0.3 * 0.01), 48000, 0.0, volume=0.01)
    assert np.max(np.abs(tiny.samples)) == pytest.approx(0.03, rel=0.05)  # capped at 10x


# ---------------------------------------------------------------------------- a whole session


class EnergyVAD:
    """Deterministic stand-in for Silero: 'speech' = loud frame."""

    def __call__(self, frame):
        return 0.95 if float(np.sqrt(np.mean(frame**2))) > 0.05 else 0.02

    def reset(self):
        pass


class FakeASR:
    name = "fake"
    device = "cpu"

    def transcribe(self, audio, prompt=None):
        return ASRResult("こんにちは、みんな", avg_logprob=-0.1, no_speech_prob=0.01, elapsed=0.01)


def sse(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\ndata: [DONE]\n\n"


class FakeLLM:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        system, user = body["messages"][0]["content"], body["messages"][-1]["content"]
        if "response_format" in body and user.startswith("["):  # chat batch
            out = json.dumps([f"EN:{t}" for t in json.loads(user)], ensure_ascii=False)
        elif "response_format" in body:
            out = "[]"
        elif "check exactly what it says" in system:
            out = "Good luck!"
        elif "My message" in user or user == "good luck":
            out = "がんばって！"
        else:
            out = "Hello everyone!"
        return httpx.Response(200, text=sse(out), headers={"content-type": "text/event-stream"})


async def wait_until(pred, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def test_host_session_subtitles_chat_and_message_box(monkeypatch):
    monkeypatch.setattr("tsuyaku.asr.worker.SileroVAD", EnergyVAD)
    fake = FakeLLM()
    sent = []

    async def run():
        cfg = Config()
        engine = Engine(cfg)
        engine.asr = FakeASR()
        engine.asr_ready.set()
        engine.llm = LLMClient("http://fake/v1", transport=httpx.MockTransport(fake))
        engine.llm_ready.set()
        engine.composer.client = engine.llm
        host = Host(cfg, sent.append, engine)
        host.start()
        assert sent[0]["type"] == "status" and sent[0]["asr"]["state"] == "ready"
        assert sent[0]["llm"]["state"] == "ready" and sent[0]["missing"] == []

        host.handle({"type": "watch", "pid": 1, "tab": 7, "video": "vid00000001", "title": "歌枠",
                     "channel": "Pekora Ch.", "channelId": "UC1"})
        # 0.4 s quiet, 1.2 s "speech", 0.8 s quiet, in the page's 4096-sample buffers from 30 s.
        audio = pcm48(0.4, amp=0.0) + pcm48(1.2) + pcm48(0.8, amp=0.0)
        step = 4096 * 2
        for i in range(0, len(audio), step):
            host.handle({"type": "audio", "pid": 1, "tab": 7, "t": 30.0 + i / 2 / 48000, "speed": 1,
                         "rate": 48000, "cont": True, "pcm": base64.b64encode(audio[i:i + step]).decode()})
        cues = lambda: [m["cue"] for m in sent if m["type"] == "cue"]  # noqa: E731
        await wait_until(lambda: any(c["status"] == "done" for c in cues()))
        done = [c for c in cues() if c["status"] == "done"][-1]
        assert done["jp"] == "こんにちは、みんな" and done["en"] == "Hello everyone!"
        assert done["start"] == pytest.approx(30.4, abs=0.2)  # on the video's timeline
        assert done["end"] == pytest.approx(31.6, abs=0.3)
        assert all(m["pid"] == 1 and m["video"] == "vid00000001" for m in sent if m["type"] == "cue")

        # Chat in the same tab: translated with the stream's title for context.
        host.handle({"type": "chat", "pid": 2, "tab": 7, "items": [{"key": "m1", "text": "家どこ？"},
                                                                   {"key": "m2", "text": "hello"}]})
        chat = lambda: {m["key"]: m for m in sent if m["type"] == "chat"}  # noqa: E731
        await wait_until(lambda: "m1" in chat() and "m2" in chat())
        assert chat()["m1"]["text"] == "EN:家どこ？" and chat()["m1"]["state"] == "done"
        assert chat()["m2"]["state"] == "none"
        chat_req = next(r for r in fake.requests if "response_format" in r)
        assert "歌枠" in chat_req["messages"][0]["content"]

        # Message box: what the streamer just said goes along as context.
        host.handle({"type": "compose", "pid": 2, "tab": 7, "seq": 3, "text": "good luck", "tone": "fan",
                     "retry": 0})
        composed = lambda: [m for m in sent if m["type"] == "composed"]  # noqa: E731
        await wait_until(lambda: composed() and composed()[-1]["result"].get("done"))
        result = composed()[-1]["result"]
        assert result["versions"][0]["ja"] == "がんばって！" and result["versions"][0]["back"] == "Good luck!"
        assert composed()[-1]["pid"] == 2 and composed()[-1]["seq"] == 3
        compose_req = next(r for r in fake.requests if "My message" in r["messages"][-1]["content"])
        assert "Hello everyone!" in compose_req["messages"][-1]["content"]

        # The tab closes: its speech thread stops.
        worker = host.pages[1].pipeline.worker
        host.handle({"type": "closed", "pid": 1})
        host.handle({"type": "closed", "pid": 2})
        assert not host.pages and not host.chats
        worker.join(2.0)
        assert not worker._thread.is_alive()
        await host.engine.aclose()

    asyncio.run(run())


def test_host_reports_missing_models_without_loading_them():
    sent = []

    async def run():
        cfg = Config()
        host = Host(cfg, sent.append, Engine(cfg))
        host.start()
        status = sent[-1]
        assert status["asr"]["state"] == "missing" and status["llm"]["state"] == "missing"
        assert len(status["missing"]) == 3 and not host._attempted
        # Chat before the translator runs: "off", so the page asks again once it is ready.
        host.handle({"type": "chat", "pid": 4, "tab": 1, "items": [{"key": "m1", "text": "こんにちは"}]})
        assert sent[-1] == {"type": "chat", "pid": 4, "key": "m1", "text": "", "state": "off"}

    asyncio.run(run())


# ---------------------------------------------------------------------------- install


def test_native_host_manifest_and_xpi(tmp_path):
    program = tmp_path / "bin" / "tsuyaku-host"
    path = install.register(program, tmp_path / "hosts", registry=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == install.HOST_NAME == "tsuyaku"
    assert data["path"] == str(program) and data["type"] == "stdio"
    assert data["allowed_extensions"] == [install.EXTENSION_ID]
    install.unregister(tmp_path / "hosts", registry=False)
    assert not path.exists()

    xpi = install.build_xpi(tmp_path / "t.xpi")
    with zipfile.ZipFile(xpi) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["browser_specific_settings"]["gecko"]["id"] == install.EXTENSION_ID
    assert not any("__pycache__" in n or n.startswith(".") for n in names)
    # Everything the manifest refers to is in the package.
    referenced = list(manifest["background"]["scripts"]) + [manifest["browser_action"]["default_popup"]]
    referenced += list(manifest["icons"].values())
    for cs in manifest["content_scripts"]:
        referenced += cs.get("js", []) + cs.get("css", [])
    assert set(referenced) <= names
    chat = next(cs for cs in manifest["content_scripts"] if "content/chat.js" in cs["js"])
    assert chat["js"].index("content/chat-bridge.js") < chat["js"].index("content/chat.js")
    assert "nativeMessaging" in manifest["permissions"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_extension_scripts_parse():
    for script in install.extension_dir().rglob("*.js"):
        subprocess.run(["node", "--check", str(script)], check=True)


def test_host_process_talks_on_stdout_and_exits_when_firefox_disconnects(tmp_home):
    """What Firefox does: start the program, read framed messages, close stdin to stop it."""
    proc = subprocess.Popen([sys.executable, "-m", "tsuyaku.host", "manifest.json", install.EXTENSION_ID],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        first = read_message(proc.stdout)
        assert first["type"] == "status" and first["asr"]["state"] == "missing"
        proc.stdin.write(encode_message({"type": "status"}))
        proc.stdin.flush()
        assert read_message(proc.stdout)["type"] == "status"
        proc.stdin.close()
        assert proc.wait(timeout=20) == 0
        assert proc.stdout.read() == b""  # nothing but messages ever reaches stdout
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------------------- settings page


def test_settings_apply_only_editable_fields_and_report_restarts():
    from tsuyaku.host import settings

    cfg = Config()
    changes = {
        "asr": {"window_s": 99, "engine": "nonsense"},  # clamped / rejected
        "llm": {"model_file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "port": 1},  # port isn't editable
        "compose": {"provider": "groq", "groq_key": "k", "versions": 7, "tone": "fan"},
        "ui": {"anything": 1},
    }
    asr, llm, compose = settings.apply(cfg, changes)
    assert cfg.asr.window_s == 30 and cfg.asr.engine == "whisper" and asr  # window changed -> reload
    assert cfg.llm.model_file.endswith("Q4_K_M.gguf") and cfg.llm.port == 8765 and llm
    assert cfg.compose.versions == 3 and cfg.compose.cloud_key("groq") == "k" and compose
    assert settings.apply(cfg, {"llm": {"temperature": 0.4}}) == (False, False, False)
    assert cfg.llm.temperature == 0.4


def test_settings_payload_and_glossary(tmp_home):
    from tsuyaku.host import settings
    from tsuyaku.mt.glossary import Glossary

    g = Glossary()
    g.ensure_channel("UC1", "Pekora Ch.")
    settings.save_glossary(g, "UC1", "Pekora Ch.", [{"jp": "ぺこら", "en": "Pekora"}, {"jp": "", "en": "x"}])
    settings.save_glossary(g, "", "", [{"jp": "配信", "en": "stream", "note": "n"}])
    g2 = Glossary()
    data = settings.payload(Config(), g2)
    assert data["settings"]["llm"]["manage_server"] is True and "port" not in data["settings"]["llm"]
    assert data["glossary"]["global"] == [{"jp": "配信", "en": "stream", "note": "n"}]
    assert data["glossary"]["channels"][0]["entries"] == [{"jp": "ぺこら", "en": "Pekora", "note": ""}]
    assert {p["key"] for p in data["providers"]} == {"gemini", "groq", "openrouter"}
    assert data["presets"]["llm"][0]["file"].endswith(".gguf")
    assert data["gpu"] is None
    data = settings.payload(Config(), g2, ("NVIDIA GeForce GTX 1080 Ti", 11.0))
    assert data["gpu"] == {"name": "NVIDIA GeForce GTX 1080 Ti", "gb": 11.0}


def test_model_presets():
    from tsuyaku.host import settings

    presets = settings.LLM_PRESETS
    assert len({(p.repo, p.file) for p in presets}) == len(presets)
    assert all(p.file.endswith(".gguf") and p.note and 0 < p.gpu_gb < 24 for p in presets)
    default = Config().llm
    assert (presets[0].repo, presets[0].file) == (default.model_repo, default.model_file)
    # Presets come grouped by GPU size, smallest first.
    groups = list(dict.fromkeys(p.group for p in presets))
    assert groups == sorted(groups, key=lambda g: int(g.split()[0].split("–")[0]))


def test_llama_server_command(tmp_path, monkeypatch):
    from tsuyaku.mt import server

    exe, model = tmp_path / "llama-server", tmp_path / "m.gguf"
    exe.write_text("")
    model.write_text("")
    cfg = Config().llm
    cfg.server_path, cfg.model_path = str(exe), str(model)
    cmd = server.LlamaServer(cfg).command()
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "auto"  # llama.cpp fits the model to the GPU
    cfg.gpu_layers = 20
    cmd = server.LlamaServer(cfg).command()
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "20"


def test_gpu_memory_from_nvidia_smi(monkeypatch):
    from tsuyaku.mt import server

    monkeypatch.setattr(server, "gpu_info", lambda: "NVIDIA GeForce GTX 1080 Ti, 11264 MiB, 581.57, 6.1")
    assert server.gpu_memory() == ("NVIDIA GeForce GTX 1080 Ti", 11.0)
    monkeypatch.setattr(server, "gpu_info", lambda: "")
    assert server.gpu_memory() is None


def test_saving_settings_restarts_the_changed_part(tmp_home, monkeypatch):
    sent = []
    calls = []

    async def run():
        cfg = Config()
        engine = Engine(cfg)
        engine.asr = FakeASR()
        engine.llm = LLMClient("http://fake/v1", transport=httpx.MockTransport(FakeLLM()))
        host = Host(cfg, sent.append, engine)
        host.start()

        def load_asr():
            calls.append("asr")
            engine.asr = FakeASR()

        monkeypatch.setattr(engine, "load_asr", load_asr)
        monkeypatch.setattr("tsuyaku.setup.components", lambda c: [])  # everything downloaded
        host.handle({"type": "saveSettings", "pid": 9, "settings": {"asr": {"window_s": 8}, "vad": {"threshold": 0.6}}})
        saved = next(m for m in sent if m["type"] == "saved")
        assert saved["ok"] and saved["restarting"] == ["speech model"] and saved["pid"] == 9
        assert host.status()["asr"]["state"] in ("loading", "ready")
        await wait_until(lambda: host.status()["asr"]["state"] == "ready")
        assert calls == ["asr"] and cfg.vad.threshold == 0.6
        assert Config.load().asr.window_s == 8  # saved to config.toml
        await engine.aclose()

    asyncio.run(run())
