# How Tsuyaku works

```
youtube.com in Firefox                                    tsuyaku-host (Python, started by the toolbar button)
  <video> ─ Web Audio tap ─ PCM + video time ──────────►  resample ─► Silero VAD ─► Whisper ─► LLM ─┐
  subtitles drawn in YouTube's player ◄── cues on the video's timeline ◄─────────────────────────────┘
  YouTube chat ─ new messages ─────────────────────────►  slang/cache/batch ─► LLM ─┐
                 translated in place ◄──────────────────────────────────────────────┘
  chat box (English) ─ ~1 s idle ──────────────────────►  LLM (or a cloud model) ─► Japanese + back-translation
                                     native messaging (stdin/stdout)        llama-server (GPU)
```

The extension side is described in [FIREFOX.md](FIREFOX.md); this page covers the pipeline.

## Latency budget

From the moment a phrase ends in the video, on a GTX 1080 Ti with the default models (averages
over 169 lines of a live stream, from the popup's "This tab" line, which shows your own numbers):

| Stage | GTX 1080 Ti | Notes |
|---|---|---|
| Phrase closes | 0.3 s pause, or forced cut at ≤3 s | quietest point in the last part of the phrase |
| Speech recognition | 0.1 s | kotoba-whisper, int8, 6 s window |
| Translation | 0.3 s | Qwen3-4B Q5_K_M; prompt prefix cached, words stream onto the screen as they arrive |

The extension hears the sound as the video plays it, so lines appear just after they are spoken
(YouTube's player can't be held back); each line then stays on screen long enough to read.

## Design notes

**Page audio on its own clock** (`host/audio.py`). The page sends ~85 ms buffers of 16-bit PCM
with the video time, playback speed and volume. The host undoes the volume (Firefox applies it
before the tap), resamples to 16 kHz with libswresample, and puts the audio on a continuous clock:
the VAD needs unbroken time, but the video's clock runs at other speeds or jumps (seek, ad, "jump to
live"). A jump leaves a 1 s gap on the clock, which closes the phrase being spoken. Every chunk
remembers its video time and speed, so recognised phrases are mapped back onto the video's timeline.

**Phrase cutting.** Silero VAD scores 32 ms frames. A phrase ends after a short pause, or, for
streamers who never pause, is cut before `max_segment_s` at the least speech-like frame. Unfinished
phrases are marked with `…` for the translator.

**Fast Whisper on short phrases.** Stock Whisper pads every input to 30 s, so a 2 s phrase costs as
much as 30 s of audio. Tsuyaku pads only to max(6 s, phrase + 2 s), which makes the encoder ~5–7×
faster. With a short window the decoder likes to "continue" into the padding and repeat itself, so
it decodes *with timestamps* and keeps only text whose segment starts inside the real audio. Known
Whisper phantoms ("ご視聴ありがとうございました" on silence), repetition loops and impossible
character rates are filtered out.

**Prompt caching.** Subtitle requests are a growing conversation (system prompt + previous lines as
user/assistant turns + the new line). It only grows, and is trimmed in blocks, so llama-server can
reuse the cached prefix and only processes the new line (~20 tokens). The system prompt is warmed
up when a video starts.

**Priorities.** One llama-server with several parallel slots serves subtitles, chat and the
message box. Chat batches wait while a subtitle is being translated, but never more than 2.5 s. When
translation falls behind, stale lines are shown in Japanese and the newest few are merged into one
request, so subtitles never drift further and further behind.

**Chat.** Canned translations for common netspeak and an LRU cache handle the bulk of messages
for free. The rest go in batches of up to 10 with a JSON-schema-constrained reply. Identical
messages in a burst are translated once, and when the backlog is old, the newest messages go first;
anything that waited more than 20 s can be translated with a click (文A). Nothing is sent while the
translator is still starting; the messages on screen are translated once it is ready.

**Message box.** Translation starts after `debounce_ms` of no typing, and in-flight requests are
cancelled (the server stops generating). The first Japanese version streams in, then two more
versions (one JSON request) and the back-translations (one request for the first, one batched for
the others) run in parallel. The back-translation never sees your English, so it shows what the
Japanese really says. Per-tone few-shot examples keep small models on track, and output that looks
Chinese is retried in Japanese. Results are cached per text, tone and retry. With a cloud model
configured, it is tried first; a rate limit, bad key or network error switches to the local model
for 10 minutes. Cloud requests leave out llama.cpp-only fields and JSON schemas (JSON is requested
in the prompt) and ask "thinking" models not to think; if a provider rejects those extra fields,
they are dropped.

**Translation models** (Hy-MT2, `mt/prompts.py`). Models made only for translating have no system
prompt and follow a handful of instructions they were trained on, one text per request. Tsuyaku
picks the prompt style from the model's name (`llm.prompt_style` overrides it). A subtitle goes in
the "background information" form, with the stream title and the last four lines (Japanese → English)
as background; glossary entries and netspeak (草 = lol, ｗｗｗ, 888…) are added as reference
translations only when they appear in the line, because a model told about a name tends to put it
into lines where it isn't. Chat messages are translated one per request, two at a time (leaving a
server slot for subtitles), instead of one JSON batch: small translation models get batches wrong.
The message box uses the "style" form with the tone; its other versions are extra samples at a
higher temperature, and back-translations use the plain form. Requests use the sampling settings
the model card recommends.

**GPU memory.** llama-server is started with `--n-gpu-layers auto`, so llama.cpp fits the model to
the free GPU memory and keeps the rest in system RAM rather than failing. The speech model is loaded
first so that it has its share. The settings page compares each preset's estimate (model + context,
+1.5 GB for Whisper on the GPU) with the card's memory reported by `nvidia-smi`.

**In YouTube's chat** (`extension/content/chat.js`). A MutationObserver finds new messages (text,
Super Chat, membership, pinned), sends their text (channel emotes as `:code:`) in batches, and
inserts the English next to the original, which is hidden. Clicking flips the two; YouTube's message
menu still opens from the author name. If YouTube re-renders a message (e.g. a moderator deletes
it), the translation is dropped and redone. In the chat box, Tab/Enter replace the text with
`execCommand('insertText')`, which YouTube treats as typing, so its send button, length counter and
Enter-to-send keep working; while the suggestions are open, Enter never sends the English by
accident.

## Module map

| Module | Purpose |
|---|---|
| `extension/*` | the Firefox extension: background (on/off, routing), popup, settings page, `content/watch.js` (audio tap + subtitles on YouTube's player), `content/chat.js` + `chat-bridge.js` (chat) |
| `host/service.py`, `host/protocol.py` | the native messaging host: per-tab subtitle pipelines, chat translators, message box, model loading and downloads |
| `host/audio.py` | page audio → 16 kHz chunks on a continuous clock, mapped back to video time |
| `host/settings.py` | what the settings page reads and changes (config.toml, glossary) |
| `host/install.py` | registers the host with Firefox (`tsuyaku firefox`), packages the extension |
| `asr/vad.py`, `asr/worker.py` | phrase segmentation, ASR thread, backlog merging |
| `asr/engines.py`, `asr/filters.py` | Whisper (short window) / ReazonSpeech, output cleanup |
| `mt/server.py`, `mt/client.py` | llama-server download/launch, streaming client + priority gate |
| `mt/translator.py`, `mt/prompts.py`, `mt/glossary.py` | subtitle/chat/message box translation, prompts for instruction and translation models |
| `mt/cloud.py` | free cloud providers for the message box: presets, model picking, reasoning settings |
| `engine.py`, `config.py`, `setup.py` | long-lived models and servers; settings; downloads |
| `session.py`, `ingest/*`, `chat/*` | the command-line tools: yt-dlp, live-edge HLS fetching, reading YouTube's live chat |

## Threading

The host runs one asyncio event loop: messages from Firefox, translation requests and replies. A
thread reads Firefox's messages from stdin. Speech recognition has its own thread per tab (one
shared model), models load in background threads, and the LLM runs in a separate `llama-server`
process. Only messages ever reach stdout: anything else that would print there goes to stderr,
which Firefox shows in its Browser Console. When Firefox closes the connection (Turn off, or
Firefox exits), the host stops llama-server and exits; on Windows, Firefox also ends the process tree.
