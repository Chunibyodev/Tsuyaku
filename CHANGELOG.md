# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## 1.0.0 — 2026-10-05

First public release.

* **Firefox add-on** for the normal youtube.com: English subtitles drawn in YouTube's own player
  (also in fullscreen, click a line for the Japanese, EN button next to CC), YouTube's live chat
  and chat replay translated in place, and an English → Japanese message box with three versions
  and back-translations.
* **Local models**, started and stopped from the toolbar button: Silero VAD + kotoba-whisper v2.0
  (or ReazonSpeech) for speech, llama.cpp for translation.
* **Popup** with model status, downloads, live per-tab statistics and the switches; **settings
  page** for the speech model, translation model, message-box model and glossary.
* **Translation models for every GPU size**, from Qwen3-4B and Gemma 4 E4B to Gemma 4 26B-A4B and
  Hy-MT2-30B-A3B, including translation-only models (Hy-MT2) with prompts in the form they were
  trained on. The settings page shows which models fit your GPU; llama.cpp keeps what doesn't fit
  in system memory.
* **Optional cloud model** for the message box (Gemini, Groq or OpenRouter free tiers) with
  automatic fallback to the local model.
* Command-line tools: `run`, `chat`, `bench`, `translate`, `doctor`, `setup`, `firefox`.
