"""Name/term glossary given to the translator (global + per channel)."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import paths

log = logging.getLogger(__name__)


@dataclass
class Entry:
    jp: str
    en: str
    note: str = ""


# Common livestream vocabulary that small models often get wrong.
DEFAULT_GLOBAL = [
    Entry("配信", "stream"),
    Entry("枠", "stream (slot)"),
    Entry("スパチャ", "Super Chat"),
    Entry("メン限", "members-only"),
    Entry("メンバーシップ", "membership"),
    Entry("同接", "concurrent viewers"),
    Entry("切り抜き", "clips"),
    Entry("推し", "oshi (favourite)"),
    Entry("リスナー", "viewers"),
    Entry("歌枠", "karaoke stream"),
    Entry("雑談", "chatting stream"),
    Entry("コラボ", "collab"),
    Entry("凸待ち", "open call-in stream"),
    Entry("初見さん", "first-time viewers"),
    Entry("アーカイブ", "archive (VOD)"),
    Entry("ガチャ", "gacha"),
]


class Glossary:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.glossary_file()
        self.global_entries: list[Entry] = []
        self.channels: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.global_entries = list(DEFAULT_GLOBAL)
            self.channels = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.global_entries = [Entry(**e) for e in data.get("global", [])]
            self.channels = {
                cid: {"name": ch.get("name", ""), "entries": [Entry(**e) for e in ch.get("entries", [])]}
                for cid, ch in data.get("channels", {}).items()
            }
        except Exception:  # noqa: BLE001
            log.exception("could not read glossary %s", self.path)
            self.global_entries = list(DEFAULT_GLOBAL)

    def save(self) -> None:
        with self._lock:
            data = {
                "global": [asdict(e) for e in self.global_entries],
                "channels": {
                    cid: {"name": ch.get("name", ""), "entries": [asdict(e) for e in ch["entries"]]}
                    for cid, ch in self.channels.items()
                },
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def channel_entries(self, channel_id: str) -> list[Entry]:
        ch = self.channels.get(channel_id)
        return list(ch["entries"]) if ch else []

    def set_channel_entries(self, channel_id: str, name: str, entries: list[Entry]) -> None:
        with self._lock:
            self.channels[channel_id] = {"name": name, "entries": entries}

    def ensure_channel(self, channel_id: str, name: str) -> None:
        if channel_id and channel_id not in self.channels:
            self.channels[channel_id] = {"name": name, "entries": []}

    def entries_for(self, channel_id: str | None) -> list[Entry]:
        merged: dict[str, Entry] = {e.jp: e for e in self.global_entries}
        if channel_id:
            for e in self.channel_entries(channel_id):
                merged[e.jp] = e
        return [e for e in merged.values() if e.jp and e.en]


def format_block(entries: list[Entry], limit: int = 60) -> str:
    """The glossary as a block for a system prompt."""
    entries = entries[-limit:]
    if not entries:
        return ""
    lines = ["Glossary (use these English spellings):"]
    for e in entries:
        note = f"  ({e.note})" if e.note else ""
        lines.append(f"- {e.jp} = {e.en}{note}")
    return "\n".join(lines)
