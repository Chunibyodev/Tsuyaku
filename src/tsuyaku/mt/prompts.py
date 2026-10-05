"""Prompt templates. Kept stable per stream so the server can reuse its prompt cache."""

from __future__ import annotations

import re

from ..config import LLMConfig
from .glossary import Entry

SUBTITLE_SYSTEM = """\
You are a professional live interpreter writing English subtitles for a Japanese livestream in real time.

Each user message is the next line of speech from the stream (Japanese, from speech recognition). \
Reply with ONLY the English subtitle for that line.

Rules:
- Natural, concise English, like good anime fansubs. Keep the speaker's tone: casual stays casual.
- Lines are cut every few seconds, so a line may be an unfinished sentence (marked with "…"). \
Translate just that fragment so it reads naturally; never add things that weren't said and never repeat earlier lines.
- Japanese often drops the subject - infer it from the previous lines.
- Speech recognition sometimes mishears words; pick the most plausible meaning in context.
- Fillers and short reactions (えー, あー, うん, はい, ね) get short equivalents ("Uh,", "Yeah.", "Right?").
- Streamers use netspeak too: 草 / w = lol, 大草原 = LMAO, てぇてぇ = so wholesome.
- Use the glossary spellings for names and terms.
- No notes, no explanations, no quotation marks, no Japanese in the output.\
"""

CHAT_SYSTEM = """\
You translate Japanese YouTube live-chat messages into natural, casual English chat.

The user sends a JSON array of chat messages. Reply with a JSON array of English strings: \
exactly one per input message, in the same order.

- Keep each translation short and chat-like. Keep emoji, kaomoji, names and :emote: codes unchanged.
- Chat slang: 草 / w / ｗｗｗ = lol, 大草原 = LMAO, 888 = 👏, おつ = good work / thanks for the stream, \
てぇてぇ = so wholesome, かわいい = cute, 初見 = first time here, 神 = godlike, ナイス = nice, ワロタ = lmao, \
きた = it's here!, ガチ恋 = real love, 助かる = (thank you, that helps), 待機 = waiting, \
よろしくお願いします (greeting) = nice to meet you.
- If a message is not Japanese, return it unchanged.\
"""

COMPOSE_SYSTEM = """\
You help an English speaker take part in a Japanese YouTube live chat.
Translate the user's English message into one natural Japanese live-chat message.

Tone: {tone}

- Translate the whole meaning of the English message; don't drop or add content.
- Never leave English words in the output unless they are names or emotes.
- Write Japanese (with hiragana/katakana), never Chinese.
- Output ONLY the Japanese message. No quotes, no romaji, no explanations, no alternatives.
- Keep it about as short as the English; live chat messages are short.
- "lol" -> 草 or w, "lmao" -> 大草原, "gg" -> おつ / GG. Keep emoji as they are.
- Use the glossary for names. Add ちゃん/さん to the streamer's name only if the English uses their name.\
"""

TONES = {
    "casual": "casual plain form (タメ口) like a regular viewer, e.g. 怖かったね / すごいじゃん",
    "polite": "polite です/ます form in every sentence, e.g. 怖かったですね / すごいですね / ありがとうございます",
    "fan": "excited, affectionate fan voice (e.g. すごすぎる！ / かわいすぎ！ / 天才！) - but keep the full meaning of the English",
}

# A few example turns per tone. Small models follow examples far better than rules.
COMPOSE_EXAMPLES = {
    "casual": [
        ("good luck!", "がんばって！"),
        ("lol that was so funny", "草 面白すぎ"),
        ("what game is this?", "これ何のゲーム？"),
        ("thanks for the stream, see you tomorrow", "配信ありがとう！また明日ね"),
        ("just got here, what did I miss?", "今来た！何があったの？"),
    ],
    "polite": [
        ("good luck!", "頑張ってください！"),
        ("lol that was so funny", "笑いました、面白かったです！"),
        ("what game is this?", "これは何のゲームですか？"),
        ("thanks for the stream, see you tomorrow", "配信ありがとうございました！また明日もよろしくお願いします"),
        ("just got here, what did I miss?", "今来ました！何があったんですか？"),
    ],
    "fan": [
        ("good luck!", "がんばれー！応援してるよ！"),
        ("lol that was so funny", "大草原 面白すぎる！"),
        ("you're so cute", "かわいすぎる！"),
        ("thanks for the stream, see you tomorrow", "配信ありがとう！明日も絶対来る！"),
        ("just got here!", "今来た！間に合った！"),
    ],
}

BACK_SYSTEM = """\
Translate this Japanese live-chat message into English so a non-Japanese speaker can check exactly what it says. \
Be faithful (do not improve it). Output only the English.\
"""


ALTERNATIVES = """\
English message: {english}
First Japanese version: {first}

Write {n} more Japanese versions of the same message, each worded differently from the first \
and from each other (different phrasing or word choice), in the same tone and with the full meaning \
of the English. Reply with ONLY a JSON array of {n} strings.\
"""

BACK_MANY_SYSTEM = """\
The user sends a JSON array of Japanese live-chat messages. Translate each one into English so a \
non-Japanese speaker can check exactly what it says. Be faithful (do not improve them). \
Reply with ONLY a JSON array of English strings: one per message, in the same order.\
"""


def stream_context(title: str, channel: str, glossary: str) -> str:
    parts = []
    if title or channel:
        parts.append(f"Stream: {title}" + (f"\nChannel: {channel}" if channel else ""))
    if glossary:
        parts.append(glossary)
    return "\n\n".join(parts)


def subtitle_system(title: str, channel: str, glossary: str) -> str:
    ctx = stream_context(title, channel, glossary)
    return SUBTITLE_SYSTEM + ("\n\n" + ctx if ctx else "")


def chat_system(title: str, channel: str, glossary: str) -> str:
    ctx = stream_context(title, channel, glossary)
    return CHAT_SYSTEM + ("\n\n" + ctx if ctx else "")


def compose_system(tone: str, title: str, channel: str, glossary: str) -> str:
    ctx = stream_context(title, channel, glossary)
    text = COMPOSE_SYSTEM.format(tone=TONES.get(tone, TONES["casual"]))
    return text + ("\n\n" + ctx if ctx else "")


# ============================================================================ translation models
# Dedicated translation models (Tencent's Hy-MT) have no system prompt and are trained on a few
# fixed instructions (see their model card): one text per request, optionally with reference
# translations for terms, background information, or a style.

CHAT = "chat"  # general instruction models (Qwen, Gemma, ...): the prompts above
TRANSLATION = "translation"  # Hy-MT

_MT_NAME = re.compile(r"hy-?mt|hunyuan-?mt", re.I)


def style_for(llm: LLMConfig) -> str:
    """How to prompt the configured model."""
    if llm.prompt_style in (CHAT, TRANSLATION):
        return llm.prompt_style
    name = (llm.model_path or llm.model_file) if llm.manage_server else llm.model_name
    return TRANSLATION if _MT_NAME.search(name or "") else CHAT


MT_PLAIN = (
    "Translate the following text into {lang}. Note that you should only output the translated result "
    "without any additional explanation:\n\n{text}"
)
MT_BACKGROUND = (
    "[Background Information]\n{background}\n\n{terms}Please translate the following text into {lang}, "
    "taking the provided background information into consideration.\n\n[Source Text]\n{text}"
)
MT_STYLE = (
    "{terms}Please translate the following text into {lang}. Note that the translation style must strictly "
    "conform to [{style}]:\n\n{text}"
)
MT_TONES = {
    "casual": "a short, casual Japanese live-chat message in plain form (タメ口)",
    "polite": "a short, polite Japanese live-chat message in です/ます form",
    "fan": "a short, excited and affectionate Japanese fan message",
}
# Live-chat netspeak, given as reference translations when it appears (a translation model
# reads 草 as "grass" otherwise).
MT_SLANG_JA = [
    (re.compile(r"大草原"), "LMAO"),
    (re.compile(r"草(?=$|[\sｗwW!！?？。、…~〜ー生])"), "lol"),
    (re.compile(r"[ｗw]{2,}"), "lol"),
    (re.compile(r"88{2,}|８８{2,}"), "👏"),
    (re.compile(r"て[ぇえ]て[ぇえ]"), "so wholesome"),
    (re.compile(r"初見"), "first time here"),
    (re.compile(r"スパチャ"), "Super Chat"),
]
MT_SLANG_EN = [
    (re.compile(r"\blmao\b", re.I), "大草原"),
    (re.compile(r"\blol\b", re.I), "草"),
]
MT_CONTEXT_LINES = 4  # previous subtitle lines given as background


def _reference(pairs: list[tuple[str, str]]) -> str:
    unique = list(dict.fromkeys(pairs))
    if not unique:
        return ""
    return "Reference the following translations:\n" + "\n".join(f"{a} translates to {b}" for a, b in unique) + "\n\n"


def _plain(en: str) -> str:
    return re.sub(r"\s*\(.*?\)", "", en).strip()  # "oshi (favourite)" -> "oshi"


def terms_from_japanese(text: str, glossary: list[Entry]) -> list[tuple[str, str]]:
    """Glossary entries and netspeak that appear in a Japanese text."""
    pairs = [(e.jp, _plain(e.en)) for e in glossary if e.jp and e.jp in text and _plain(e.en)]
    for pattern, en in MT_SLANG_JA:
        pairs += [(m.group(0), en) for m in pattern.finditer(text)
                  if not any(m.group(0) in jp for jp, _ in pairs)]  # 初見さん (glossary) covers 初見
    return pairs


def terms_from_english(text: str, glossary: list[Entry]) -> list[tuple[str, str]]:
    """Glossary entries (by their English) and netspeak that appear in an English text."""
    pairs = []
    for e in glossary:
        en = _plain(e.en)
        if en and re.search(rf"(?<!\w){re.escape(en)}(?!\w)", text, re.I):
            pairs.append((en, e.jp))
    for pattern, ja in MT_SLANG_EN:
        pairs += [(m.group(0), ja) for m in pattern.finditer(text)]
    return pairs


def _stream(title: str, channel: str) -> str:
    if title and channel:
        return f'a YouTube live stream, "{title}" by {channel}'
    if title or channel:
        return f'a YouTube live stream, "{title or channel}"'
    return "a Japanese YouTube live stream"


def mt_subtitle(line: str, history: list[tuple[str, str]], title: str, channel: str, glossary: list[Entry]) -> str:
    background = (f"Lines of speech from {_stream(title, channel)}, from speech recognition (words may be "
                  "misheard). A line ending in \"…\" is unfinished.")
    if history:
        background += "\nPrevious lines:\n" + "\n".join(f"{jp} → {en}" for jp, en in history[-MT_CONTEXT_LINES:])
    return MT_BACKGROUND.format(background=background, terms=_reference(terms_from_japanese(line, glossary)),
                                lang="English", text=line)


def mt_chat(text: str, title: str, channel: str, glossary: list[Entry]) -> str:
    background = f"A message from the live chat of {_stream(title, channel)}. Viewers write short, casual messages."
    return MT_BACKGROUND.format(background=background, terms=_reference(terms_from_japanese(text, glossary)),
                                lang="English", text=text)


def mt_compose(english: str, tone: str, glossary: list[Entry]) -> str:
    return MT_STYLE.format(terms=_reference(terms_from_english(english, glossary)), lang="Japanese",
                           style=MT_TONES.get(tone, MT_TONES["casual"]), text=english)


def mt_back(japanese: str, glossary: list[Entry]) -> str:
    return _reference(terms_from_japanese(japanese, glossary)) + MT_PLAIN.format(lang="English", text=japanese)
