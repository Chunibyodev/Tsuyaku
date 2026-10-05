import asyncio
import json

import httpx

from tsuyaku.mt.client import LLMClient
from tsuyaku.mt.cloud import PROVIDERS, CloudModel, pick_model, reasoning_params
from tsuyaku.mt.translator import ComposeTranslator


def sse(parts):
    lines = ["data: " + json.dumps({"choices": [{"delta": {"content": p}}]}) for p in parts]
    return "\n\n".join([*lines, "data: [DONE]"]) + "\n\n"


def responder_for(body: dict) -> str:
    """Pretend model: answers each composer request by what it asks for."""
    system = body["messages"][0]["content"]
    user = body["messages"][-1]["content"]
    if "JSON array of Japanese live-chat messages" in system:
        n = len(json.loads(user))
        return json.dumps([f"back {i}" for i in range(n)])
    if "more Japanese versions" in user:
        return json.dumps(["頑張ってね！", "ファイト！"])
    if "check exactly what it says" in system:
        return "Good luck!"
    return "がんばって！"


class Server:
    def __init__(self, status: int = 200):
        self.status = status
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status != 200:
            return httpx.Response(self.status, text='{"error": "nope"}')
        return httpx.Response(200, text=sse([responder_for(body)]), headers={"content-type": "text/event-stream"})


def client(server: Server, **kw) -> LLMClient:
    return LLMClient("http://fake/v1", transport=httpx.MockTransport(server), **kw)


def test_three_versions_with_back_translations():
    server = Server()
    updates = []

    async def run():
        tr = ComposeTranslator(client(server))
        return await tr.compose("good luck!", on_update=updates.append)

    result = asyncio.run(run())
    assert result.done
    assert [v.ja for v in result.versions] == ["がんばって！", "頑張ってね！", "ファイト！"]
    assert result.versions[0].back == "Good luck!"
    assert [v.back for v in result.versions[1:]] == ["back 0", "back 1"]
    assert updates[0].versions[0].ja  # the first version is shown before the rest
    assert updates[-1].done
    # Local server: JSON-schema output and llama.cpp extensions are used.
    assert any("response_format" in b for b in server.bodies)
    assert all(b.get("cache_prompt") for b in server.bodies)


def test_cached_result_is_reused():
    server = Server()

    async def run():
        tr = ComposeTranslator(client(server))
        await tr.compose("good luck!")
        n = len(server.bodies)
        await tr.compose("good luck!")
        return n

    n = asyncio.run(run())
    assert len(server.bodies) == n


def test_cloud_failure_falls_back_to_local():
    cloud_server = Server(status=429)
    local_server = Server()

    async def run():
        cloud = CloudModel(PROVIDERS["groq"], "key", "some-model")
        cloud._client = client(cloud_server, local=False)
        tr = ComposeTranslator(client(local_server), cloud)
        result = await tr.compose("good luck!", versions=1)
        return tr, result

    tr, result = asyncio.run(run())
    assert result.versions[0].ja == "がんばって！"
    assert result.source == "local model"
    assert "rate limit" in result.note
    assert tr.describe() == "local model"  # cloud is skipped for a while after a rate limit


def test_cloud_requests_have_no_llama_extensions_and_drop_rejected_extras():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, text='{"error": "unknown field reasoning_effort"}')
        return httpx.Response(200, text=sse(["がんばって！"]), headers={"content-type": "text/event-stream"})

    async def run():
        c = LLMClient("http://fake/v1", transport=httpx.MockTransport(handler), local=False,
                      extra_body={"reasoning_effort": "none"})
        return await c.chat([{"role": "user", "content": "hi"}], json_schema={"type": "array"})

    result = asyncio.run(run())
    assert result.text == "がんばって！"
    assert len(calls) == 2 and "reasoning_effort" not in calls[1]
    assert all("cache_prompt" not in b and "response_format" not in b for b in calls)


def test_pick_model():
    gemini = ["models/gemini-2.5-flash", "models/gemini-3.5-flash", "models/gemini-3.5-flash-lite",
              "models/gemini-3.5-flash-preview-tts", "models/gemini-3.5-pro", "models/text-embedding-004"]
    assert pick_model(gemini, PROVIDERS["gemini"]) == "gemini-3.5-flash"
    assert pick_model(gemini + ["models/gemini-flash-latest"], PROVIDERS["gemini"]) == "gemini-flash-latest"
    groq = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "qwen/qwen3-32b", "whisper-large-v3"]
    assert pick_model(groq, PROVIDERS["groq"]) == "qwen/qwen3-32b"
    router = ["deepseek/deepseek-chat-v3.1", "deepseek/deepseek-chat-v3.1:free", "deepseek/deepseek-r1:free",
              "meta-llama/llama-3.3-70b-instruct:free"]
    assert pick_model(router, PROVIDERS["openrouter"]) == "deepseek/deepseek-chat-v3.1:free"
    assert pick_model(["whisper-large-v3"], PROVIDERS["groq"]) is None


def test_reasoning_params():
    assert reasoning_params("gemini", "gemini-2.5-flash") == {"reasoning_effort": "none"}
    assert reasoning_params("groq", "qwen/qwen3-32b")["reasoning_effort"] == "none"
    assert reasoning_params("groq", "llama-3.3-70b-versatile") == {}
