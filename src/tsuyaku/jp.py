"""Small helpers for detecting and normalising Japanese text."""

from __future__ import annotations

import re
import unicodedata

_KANA = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿｦ-ﾟ]")
_KANJI = re.compile(r"[一-鿿㐀-䶿]")
_HANGUL = re.compile(r"[가-힯]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_ONLY_LAUGH = re.compile(r"^[wWｗＷ]+$")
_ONLY_888 = re.compile(r"^[8８]{3,}$")
_SPACE = re.compile(r"\s+")


def has_kana(text: str) -> bool:
    return bool(_KANA.search(text))


def has_kanji(text: str) -> bool:
    return bool(_KANJI.search(text))


def is_japanese(text: str) -> bool:
    """True if the text looks like it needs JP->EN translation.

    Kana is a sure sign. Kanji without kana is usually Japanese in a JP stream's chat
    (e.g. 草, 神回), so it counts too unless Hangul is present.
    """
    if has_kana(text):
        return True
    if has_kanji(text) and not _HANGUL.search(text):
        return True
    return False


def normalize(text: str) -> str:
    """NFKC + whitespace collapse; used as the translation cache key."""
    text = unicodedata.normalize("NFKC", text)
    return _SPACE.sub(" ", text).strip()


# Chat netspeak that has a fixed meaning; translated without asking the LLM.
CHAT_SLANG: dict[str, str] = {
    "草": "lol",
    "くさ": "lol",
    "大草原": "LMAO",
    "草草": "lol lol",
    "おつ": "Good work!",
    "乙": "Good work!",
    "おつかれ": "Good work!",
    "おつかれさま": "Thanks for the stream!",
    "お疲れ様": "Thanks for the stream!",
    "お疲れ様です": "Thanks for the stream!",
    "お疲れ様でした": "Thanks for the stream!",
    "おつかれさまでした": "Thanks for the stream!",
    "かわいい": "Cute!",
    "可愛い": "Cute!",
    "かわいい！": "Cute!",
    "きたー": "It's here!",
    "キタ━━━━(ﾟ∀ﾟ)━━━━!!": "It's here!!",
    "こんばんは": "Good evening!",
    "こんにちは": "Hello!",
    "おはよう": "Good morning!",
    "おはようございます": "Good morning!",
    "おやすみ": "Good night!",
    "おやすみなさい": "Good night!",
    "ありがとう": "Thank you!",
    "ありがとうございます": "Thank you!",
    "てぇてぇ": "So precious (wholesome)",
    "てえてえ": "So precious (wholesome)",
    "尊い": "Precious!",
    "えらい": "Good job!",
    "偉い": "Good job!",
    "すごい": "Amazing!",
    "すご": "Wow",
    "やば": "Whoa",
    "やばい": "Insane!",
    "ナイス": "Nice!",
    "ないす": "Nice!",
    "初見": "First time here!",
    "初見です": "First time here!",
    "待機": "Waiting!",
    "わこつ": "Congrats on going live!",
    "ｗ": "lol",
}


def quick_chat_translation(text: str) -> str | None:
    """Return a canned translation for very common chat messages, else None."""
    key = normalize(text)
    if not key:
        return None
    if _ONLY_LAUGH.match(key):
        return "lol" if len(key) < 4 else "LOL"
    if _ONLY_888.match(key):
        return "👏👏👏"
    stripped = key.rstrip("!！?？~〜ー ")
    for candidate in (key, stripped):
        if candidate in CHAT_SLANG:
            return CHAT_SLANG[candidate]
    return None


# Simplified-Chinese-only characters that never appear in Japanese text.
_SIMPLIFIED = set("们这说时个乐话还为东么样开车门发对吗过见无长张让给头运动问贝页关买卖历层岁边办")


def looks_chinese(text: str) -> bool:
    """Heuristic: CJK text with no kana, or with simplified-only characters."""
    if any(ch in _SIMPLIFIED for ch in text):
        return True
    return has_kanji(text) and not has_kana(text) and len(_KANJI.findall(text)) >= 4


def looks_english(text: str) -> bool:
    return bool(_LATIN_WORD.search(text)) and not is_japanese(text)


def char_rate(text: str, seconds: float) -> float:
    """Characters per second (used to reject implausible ASR output)."""
    if seconds <= 0:
        return 0.0
    return len(text.replace(" ", "")) / seconds
