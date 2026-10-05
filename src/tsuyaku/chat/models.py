"""Chat message model and parsing of YouTube's live chat renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Run:
    text: str = ""
    emoji_url: str = ""
    emoji_alt: str = ""  # ":shortcut:" for channel emotes, the character for unicode emoji
    is_custom: bool = False


@dataclass
class Badge:
    label: str
    icon_type: str = ""  # OWNER, MODERATOR, VERIFIED ...
    image_url: str = ""  # member badge


@dataclass
class ChatMessage:
    id: str
    kind: str  # text, paid, sticker, membership, gift, redemption
    author: str
    runs: list[Run] = field(default_factory=list)
    author_channel_id: str = ""
    author_photo: str = ""
    badges: list[Badge] = field(default_factory=list)
    timestamp: float = 0.0  # unix seconds
    amount: str = ""
    header_color: int | None = None
    body_color: int | None = None
    header_text: str = ""
    sticker_url: str = ""
    # Translation state, owned by the app.
    translation: str | None = None
    translation_state: str = "none"  # none, pending, done, skipped, not_needed, error
    show_original: bool = False
    received_at: float = 0.0

    @property
    def is_owner(self) -> bool:
        return any(b.icon_type == "OWNER" for b in self.badges)

    @property
    def is_moderator(self) -> bool:
        return any(b.icon_type == "MODERATOR" for b in self.badges)

    @property
    def is_member(self) -> bool:
        return any(b.image_url for b in self.badges)

    @property
    def plain_text(self) -> str:
        out = []
        for r in self.runs:
            if r.text:
                out.append(r.text)
            elif r.emoji_alt:
                out.append(r.emoji_alt if not r.is_custom else f" {r.emoji_alt} ")
        return "".join(out).strip()

    @property
    def translatable_text(self) -> str:
        """Text without channel emotes (those are images, not words)."""
        out = []
        for r in self.runs:
            if r.text:
                out.append(r.text)
            elif r.emoji_alt and not r.is_custom:
                out.append(r.emoji_alt)
        return "".join(out).strip()

    @property
    def custom_emoji(self) -> list[Run]:
        return [r for r in self.runs if r.emoji_url and r.is_custom]


def _text(obj: Any) -> str:
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    return "".join(r.get("text", "") or r.get("emoji", {}).get("emojiId", "") for r in obj.get("runs", []))


def _thumb(obj: Any) -> str:
    try:
        thumbs = obj["thumbnails"]
        url = thumbs[-1]["url"]
        return "https:" + url if url.startswith("//") else url
    except (KeyError, IndexError, TypeError):
        return ""


def parse_runs(message: Any) -> list[Run]:
    runs: list[Run] = []
    if not message:
        return runs
    if "simpleText" in message:
        return [Run(text=message["simpleText"])]
    for r in message.get("runs", []):
        if "text" in r:
            runs.append(Run(text=r["text"]))
        elif "emoji" in r:
            e = r["emoji"]
            custom = bool(e.get("isCustomEmoji"))
            shortcuts = e.get("shortcuts") or []
            alt = (shortcuts[0] if custom and shortcuts else e.get("emojiId", "")) or ""
            runs.append(Run(emoji_url=_thumb(e.get("image", {})), emoji_alt=alt, is_custom=custom))
    return runs


def parse_badges(renderer: dict) -> list[Badge]:
    badges = []
    for b in renderer.get("authorBadges", []) or []:
        br = b.get("liveChatAuthorBadgeRenderer", {})
        badges.append(
            Badge(
                label=br.get("tooltip", ""),
                icon_type=(br.get("icon") or {}).get("iconType", ""),
                image_url=_thumb(br.get("customThumbnail", {})),
            )
        )
    return badges


_KINDS = {
    "liveChatTextMessageRenderer": "text",
    "liveChatPaidMessageRenderer": "paid",
    "liveChatPaidStickerRenderer": "sticker",
    "liveChatMembershipItemRenderer": "membership",
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer": "gift",
    "liveChatSponsorshipsGiftRedemptionAnnouncementRenderer": "redemption",
}


def parse_item(item: dict, received_at: float = 0.0) -> ChatMessage | None:
    for key, kind in _KINDS.items():
        r = item.get(key)
        if r is None:
            continue
        if kind == "gift":
            header = (r.get("header") or {}).get("liveChatSponsorshipsHeaderRenderer", {})
            return ChatMessage(
                id=r.get("id", ""),
                kind=kind,
                author=_text(header.get("authorName")),
                author_channel_id=r.get("authorExternalChannelId", ""),
                author_photo=_thumb(header.get("authorPhoto", {})),
                badges=parse_badges(header),
                header_text=_text(header.get("primaryText")),
                timestamp=int(r.get("timestampUsec", 0) or 0) / 1e6,
                received_at=received_at,
            )
        msg = ChatMessage(
            id=r.get("id", ""),
            kind=kind,
            author=_text(r.get("authorName")),
            runs=parse_runs(r.get("message")),
            author_channel_id=r.get("authorExternalChannelId", ""),
            author_photo=_thumb(r.get("authorPhoto", {})),
            badges=parse_badges(r),
            timestamp=int(r.get("timestampUsec", 0) or 0) / 1e6,
            amount=_text(r.get("purchaseAmountText")),
            header_color=r.get("headerBackgroundColor") or r.get("backgroundColor"),
            body_color=r.get("bodyBackgroundColor") or r.get("backgroundColor"),
            received_at=received_at,
        )
        if kind == "membership":
            primary = _text(r.get("headerPrimaryText"))
            sub = _text(r.get("headerSubtext"))
            msg.header_text = " ".join(t for t in (primary, sub) if t)
        if kind == "sticker":
            msg.sticker_url = _thumb(r.get("sticker") or {})
        return msg
    return None


@dataclass
class ChatUpdate:
    added: list[ChatMessage] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    deleted_authors: list[str] = field(default_factory=list)
    replaced: list[tuple[str, ChatMessage]] = field(default_factory=list)


def parse_actions(actions: list[dict], received_at: float = 0.0) -> ChatUpdate:
    up = ChatUpdate()
    for a in actions or []:
        if "addChatItemAction" in a:
            msg = parse_item(a["addChatItemAction"].get("item", {}), received_at)
            if msg and msg.id:
                up.added.append(msg)
        elif "markChatItemAsDeletedAction" in a:
            target = a["markChatItemAsDeletedAction"].get("targetItemId")
            if target:
                up.deleted.append(target)
        elif "markChatItemsByAuthorAsDeletedAction" in a:
            cid = a["markChatItemsByAuthorAsDeletedAction"].get("externalChannelId")
            if cid:
                up.deleted_authors.append(cid)
        elif "replaceChatItemAction" in a:
            r = a["replaceChatItemAction"]
            msg = parse_item(r.get("replacementItem", {}), received_at)
            if msg and r.get("targetItemId"):
                up.replaced.append((r["targetItemId"], msg))
    return up
