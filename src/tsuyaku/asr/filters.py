"""Clean-up for speech recognition output.

Whisper was trained on lots of subtitled video, so on silence, music or noise it likes to
invent the closing lines of a video ("thanks for watching", "please subscribe"). Those are
dropped unless the model is confident there really was speech.
"""

from __future__ import annotations

import re

from .. import jp

# Phrases Whisper invents on non-speech audio (Japanese).
HALLUCINATION_PATTERNS = [
    r"ご視聴(いただき)?(まことに|本当に)?ありがとうございま(す|した)",
    r"最後までご(視聴|覧)いただきありがとうございま(す|した)",
    r"チャンネル登録(と高評価)?(を)?(よろしく)?お願いします",
    r"高評価(と)?(チャンネル登録)?(を)?(よろしく)?お願いします",
    r"字幕(作成|制作|提供)",
    r"(ご覧|ご視聴)いただき",
    r"次回もお楽しみに",
    r"お疲れ様でした。?$",
    r"^(ん|う|あ|え)+[。、]?$",
    r"^[\s。、・…!?！？]+$",
]
_HALLUCINATION_RX = [re.compile(p) for p in HALLUCINATION_PATTERNS]

_REPEAT_CHAR = re.compile(r"(.)\1{6,}")
_REPEAT_PHRASE = re.compile(r"(.{2,12}?)\1{3,}")
_SPACE = re.compile(r"\s+")


def collapse_repeats(text: str) -> str:
    """Cut runaway repetition loops ("そうそうそうそうそう…") down to a sane length."""
    text = _REPEAT_CHAR.sub(lambda m: m.group(1) * 4, text)
    text = _REPEAT_PHRASE.sub(lambda m: m.group(1) * 2, text)
    return text


_PUNCT = re.compile(r"[\s。、・…!?！？,.]")


def _kana_fold(text: str) -> str:
    """Katakana -> hiragana and drop punctuation, for fuzzy comparisons."""
    out = []
    for ch in _PUNCT.sub("", text):
        code = ord(ch)
        out.append(chr(code - 0x60) if 0x30A1 <= code <= 0x30F6 else ch)
    return "".join(out)


def collapse_self_repeat(text: str) -> str:
    """'ABCABC' or 'ABC、ABC' (the whole phrase said twice) -> 'ABC'."""
    from difflib import SequenceMatcher

    folded = _kana_fold(text)
    n = len(folded)
    if n < 6:
        return text
    for parts in (3, 2):
        size = n // parts
        if size < 3:
            continue
        chunks = [folded[i * size:(i + 1) * size] for i in range(parts)]
        if all(SequenceMatcher(None, chunks[0], c).ratio() >= 0.8 for c in chunks[1:]):
            # Cut the original text at the position matching the first chunk's end.
            seen = 0
            for idx, ch in enumerate(text):
                if not _PUNCT.match(ch):
                    seen += 1
                if seen >= size:
                    return text[: idx + 1]
    return text


def cut_repeated_tail(text: str, min_len: int = 6) -> str:
    """'ABCD…BCD' -> 'ABCD…': drop a tail that merely copies an earlier part of the text.

    Catches decoder loops that restart mid-phrase and get cut off by the length limit.
    """
    n = len(text)
    for i in range(max(min_len, n // 4), n - min_len + 1):
        tail = text[i:]
        if len(_PUNCT.sub("", tail)) >= min_len and tail in text[:i]:
            return text[:i].rstrip("、, ")
    return text


def looks_hallucinated(text: str) -> bool:
    return any(rx.search(text) for rx in _HALLUCINATION_RX)


def clean(
    text: str,
    duration: float,
    *,
    avg_logprob: float | None = None,
    no_speech_prob: float | None = None,
    speech_ratio: float = 1.0,
    max_chars_per_second: float = 16.0,
) -> str | None:
    """Return cleaned text, or None if the result should be discarded."""
    text = _SPACE.sub(" ", text).strip()
    if not text:
        return None
    text = cut_repeated_tail(collapse_self_repeat(collapse_repeats(text)))
    stripped = re.sub(r"[\s。、・…!?！？「」『』()（）]", "", text)
    if not stripped:
        return None

    uncertain = (
        (no_speech_prob is not None and no_speech_prob > 0.5)
        or (avg_logprob is not None and avg_logprob < -0.9)
        or speech_ratio < 0.35
    )
    if looks_hallucinated(text) and (uncertain or duration < 1.5 or speech_ratio < 0.6):
        return None
    if no_speech_prob is not None and no_speech_prob > 0.8 and (avg_logprob or 0) < -0.6:
        return None
    if avg_logprob is not None and avg_logprob < -1.4:
        return None

    # Far more characters than anyone can say in this time = a repetition loop.
    rate = jp.char_rate(stripped, duration)
    if duration > 0 and rate > max_chars_per_second:
        limit = max(4, int(max_chars_per_second * duration))
        text = text[:limit]
    return text
