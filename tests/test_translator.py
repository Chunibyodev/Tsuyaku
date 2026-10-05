import asyncio
import json

import httpx

from tsuyaku.cues import Cue, CueStore
from tsuyaku.mt.client import LLMClient
from tsuyaku.mt.translator import (
    SKIPPED,
    ChatTranslator,
    ComposeTranslator,
    StreamMeta,
    SubtitleTranslator,
    parse_json_list,
    tidy_line,
)


def sse(text_parts, usage=True):
    lines = []
    for part in text_parts:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": part}}]}))
    if usage:
        lines.append("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                                            "timings": {"cache_n": 7}}))
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


class FakeLLM:
    def __init__(self, responder):
        self.requests = []
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        parts = self.responder(body)
        return httpx.Response(200, text=sse(parts), headers={"content-type": "text/event-stream"})


def client_for(fake):
    return LLMClient("http://fake/v1", transport=httpx.MockTransport(fake))


def test_tidy_line():
    assert tidy_line('English: "Hello there"') == "Hello there"
    assert tidy_line("Line one\nline two") == "Line one line two"
    assert tidy_line('Here’s the exact translation: **"Good luck in the game!"** *(or "Go for it!")*') == \
        "Good luck in the game!"
    assert tidy_line("Here's the translation:\nPlease keep going!") == "Please keep going!"
    assert tidy_line("Here is my translation of the game") == "Here is my translation of the game"


def test_subtitle_translation_streams_and_keeps_context():
    fake = FakeLLM(lambda body: ["Hel", "lo!"])
    updates = []

    async def run():
        store = CueStore()
        tr = SubtitleTranslator(client_for(fake), store, context_lines=2, on_update=lambda c: updates.append(c.en))
        tr.set_context(StreamMeta("title", "chan"), [])
        for i in range(5):
            cue = store.add(Cue(start=i, end=i + 1, jp=f"行{i}", forced_cut=(i == 0)))
            await tr.translate(cue)
            assert cue.en == "Hello!" and cue.status == "done"
        return tr

    tr = asyncio.run(run())
    assert "Hel" in updates  # partial update was streamed
    first = fake.requests[0]["messages"]
    assert first[-1]["content"].endswith("…")  # forced cut marked as unfinished
    assert fake.requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    # Context: history grows, then is trimmed in a block.
    assert len(tr.history) <= 2 + 4
    last = fake.requests[-1]["messages"]
    assert last[0]["role"] == "system" and last[-1]["content"] == "行4"


def test_subtitle_backlog_merges_and_drops_stale():
    fake = FakeLLM(lambda body: ["merged"])

    async def run():
        import time

        store = CueStore()
        tr = SubtitleTranslator(client_for(fake), store)
        now = time.time()
        old = store.add(Cue(start=0, end=1, jp="古い", arrived_at=now - 60))
        a = store.add(Cue(start=1, end=2, jp="あ", arrived_at=now))
        b = store.add(Cue(start=2, end=3, jp="い", arrived_at=now))
        for c in (old, a, b):
            tr.submit(c)
        task = asyncio.create_task(tr.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if a.status == "done":
                break
        task.cancel()
        return old, a, b

    old, a, b = asyncio.run(run())
    assert old.status == "error"  # too late: shown in Japanese
    assert a.jp == "あ い" and a.en == "merged"
    assert b.status == "merged"


def test_chat_translator_batches_and_uses_cache():
    def respond(body):
        texts = json.loads(body["messages"][-1]["content"])
        assert "response_format" in body
        return [json.dumps([f"EN:{t}" for t in texts], ensure_ascii=False)]

    fake = FakeLLM(respond)
    results = {}

    async def run():
        tr = ChatTranslator(client_for(fake), lambda mid, en: results.__setitem__(mid, en), max_batch=10)
        task = asyncio.create_task(tr.run())
        tr.submit("1", "草")  # canned
        tr.submit("2", "hello")  # not Japanese
        tr.submit("3", "家どこ？")
        tr.submit("4", "家どこ？")  # duplicate in the same burst
        tr.submit("5", "かっこいい技")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(results) == 5:
                break
        tr.submit("6", "家どこ？")  # now cached
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert results["1"] == "lol"
    assert results["2"] is None
    assert results["3"] == results["4"] == results["6"] == "EN:家どこ？"
    assert results["5"] == "EN:かっこいい技"
    assert len(fake.requests) == 1
    assert json.loads(fake.requests[0]["messages"][-1]["content"]) == ["家どこ？", "かっこいい技"]


def test_chat_translator_skips_old_messages():
    results = {}

    async def run():
        tr = ChatTranslator(client_for(FakeLLM(lambda b: ["[]"])), lambda m, e: results.__setitem__(m, e),
                            max_age_s=0.0)
        task = asyncio.create_task(tr.run())
        tr.submit("1", "古いメッセージです")
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(run())
    assert results["1"] == SKIPPED


def test_parse_json_list():
    assert parse_json_list('```json\n["a", "b"]\n```', 2) == ["a", "b"]
    assert parse_json_list('["a"]', 2) == ["a", ""]
    assert parse_json_list("1. a\n2. b", 2) == ["a", "b"]


def test_compose_retries_when_output_is_chinese():
    answers = iter([["生日快乐！"], ["誕生日おめでとう！"]])
    fake = FakeLLM(lambda body: next(answers))

    async def run():
        comp = ComposeTranslator(client_for(fake))
        return await comp.to_japanese("happy birthday!", "casual")

    assert asyncio.run(run()) == "誕生日おめでとう！"
    assert len(fake.requests) == 2
    # Few-shot examples come before the user's message.
    msgs = fake.requests[0]["messages"]
    assert msgs[1]["role"] == "user" and msgs[2]["role"] == "assistant"


# ---------------------------------------------------------------- translation models (Hy-MT)


def mt_client(fake):
    return LLMClient("http://fake/v1", transport=httpx.MockTransport(fake), style="translation")


def test_prompt_style_follows_the_model_name():
    from tsuyaku.config import LLMConfig
    from tsuyaku.mt import prompts

    cfg = LLMConfig()
    assert prompts.style_for(cfg) == "chat"
    cfg.model_file = "Hy-MT2-7B-Q4_K_M.gguf"
    assert prompts.style_for(cfg) == "translation"
    cfg.model_path = r"C:\models\qwen3-8b.gguf"  # a file on disk overrides the download
    assert prompts.style_for(cfg) == "chat"
    cfg.manage_server, cfg.model_name = False, "hy-mt2:30b"  # Ollama & co.: by the model name
    assert prompts.style_for(cfg) == "translation"
    cfg.prompt_style = "chat"
    assert prompts.style_for(cfg) == "chat"


def test_translation_model_terms_only_for_what_appears():
    from tsuyaku.mt import prompts
    from tsuyaku.mt.glossary import Entry

    g = [Entry("ぺこら", "Pekora"), Entry("配信", "stream"), Entry("推し", "oshi (favourite)")]
    assert prompts.terms_from_japanese("ぺこらの配信マジで草ｗｗ", g) == [
        ("ぺこら", "Pekora"), ("配信", "stream"), ("草", "lol"), ("ｗｗ", "lol")]
    assert prompts.terms_from_japanese("草原に行こう", g) == []  # grass is grass
    assert prompts.terms_from_japanese("推しの初見さん", g + [Entry("初見さん", "first-time viewers")]) == [
        ("推し", "oshi"), ("初見さん", "first-time viewers")]  # no notes; 初見 is covered
    assert prompts.terms_from_english("lol is the stream over? my oshi", g) == [
        ("stream", "配信"), ("oshi", "推し"), ("lol", "草")]
    assert prompts.terms_from_english("streamer", g) == []


def test_subtitles_with_a_translation_model():
    from tsuyaku.mt.glossary import Entry

    fake = FakeLLM(lambda body: ["Thanks for the Super Chat!"])

    async def run():
        store = CueStore()
        tr = SubtitleTranslator(mt_client(fake), store, context_lines=6)
        tr.set_context(StreamMeta("Minecraft", "Pekora Ch."), [Entry("スパチャ", "Super Chat"), Entry("ぺこら", "Pekora")])
        for i in range(6):
            cue = store.add(Cue(start=i, end=i + 1, jp=f"スパチャありがとう{i}"))
            await tr.translate(cue)
            assert cue.en == "Thanks for the Super Chat!"

    asyncio.run(run())
    body = fake.requests[-1]
    assert [m["role"] for m in body["messages"]] == ["user"]  # no system prompt
    prompt = body["messages"][0]["content"]
    assert prompt.startswith("[Background Information]") and prompt.endswith("[Source Text]\nスパチャありがとう5")
    assert '"Minecraft" by Pekora Ch.' in prompt
    assert "スパチャ translates to Super Chat" in prompt and "Pekora" not in prompt.split("Previous lines")[1]
    assert prompt.count(" → ") == 4  # the last few lines as context
    assert body["top_k"] == 20 and body["repeat_penalty"] == 1.05


def test_chat_with_a_translation_model_sends_one_message_per_request():
    def respond(body):
        return ["Translated " + body["messages"][0]["content"].rsplit("\n", 1)[-1]]

    fake = FakeLLM(respond)
    results = {}

    async def run():
        tr = ChatTranslator(mt_client(fake), lambda mid, en: results.__setitem__(mid, en))
        task = asyncio.create_task(tr.run())
        for i, text in enumerate(["家どこ？", "かっこいい技", "クリーパーきたｗｗｗ"]):
            tr.submit(str(i), text)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(results) == 3:
                break
        task.cancel()

    asyncio.run(run())
    assert results == {"0": "Translated 家どこ？", "1": "Translated かっこいい技", "2": "Translated クリーパーきたｗｗｗ"}
    assert len(fake.requests) == 3 and all("response_format" not in b for b in fake.requests)
    assert "ｗｗｗ translates to lol" in fake.requests[2]["messages"][0]["content"]


def test_message_box_with_a_translation_model():
    answers = iter(["がんばって！", "がんばって！", "頑張れ！", "ファイト！", "Good luck!", "Do your best!", "Fight!"])
    fake = FakeLLM(lambda body: [next(answers)])

    async def run():
        comp = ComposeTranslator(mt_client(fake))
        return await comp.compose("good luck lol", "polite", versions=3)

    result = asyncio.run(run())
    assert [v.ja for v in result.versions] == ["がんばって！", "頑張れ！", "ファイト！"]  # repeats dropped
    assert all(v.back for v in result.versions)
    first = fake.requests[0]["messages"]
    assert len(first) == 1 and "です/ます" in first[0]["content"] and "lol translates to 草" in first[0]["content"]
    back = fake.requests[-1]["messages"][0]["content"]
    assert back.startswith("Translate the following text into English")
