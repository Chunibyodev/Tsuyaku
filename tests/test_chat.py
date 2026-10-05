import time

from tsuyaku.chat.innertube import ChatPage, extract_initial_data, extract_ytcfg, sid_authorization
from tsuyaku.chat.models import parse_actions

TEXT_ACTION = {
    "clickTrackingParams": "x",
    "addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "id": "m1",
        "message": {"runs": [
            {"text": "こんにちは"},
            {"emoji": {"emojiId": "UC/abc", "shortcuts": [":_wave:"], "isCustomEmoji": True,
                       "image": {"thumbnails": [{"url": "https://yt3.ggpht.com/e1"}]}}},
            {"emoji": {"emojiId": "😀", "shortcuts": [":grin:"], "image": {"thumbnails": [{"url": "//x/g.png"}]}}},
        ]},
        "authorName": {"simpleText": "@viewer"},
        "authorExternalChannelId": "UC1",
        "authorBadges": [{"liveChatAuthorBadgeRenderer": {
            "customThumbnail": {"thumbnails": [{"url": "https://yt3/badge"}]}, "tooltip": "Member (1 year)"}}],
        "timestampUsec": "1791105148610496",
    }}},
}
PAID_ACTION = {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {
    "id": "p1", "message": {"runs": [{"text": "がんばって"}]}, "authorName": {"simpleText": "@fan"},
    "purchaseAmountText": {"simpleText": "¥500"}, "headerBackgroundColor": 4278239141,
    "bodyBackgroundColor": 4280150454, "timestampUsec": "1"}}}}
MEMBER_ACTION = {"addChatItemAction": {"item": {"liveChatMembershipItemRenderer": {
    "id": "j1", "authorName": {"simpleText": "@new"}, "headerSubtext": {"runs": [{"text": "Welcome to "},
                                                                                  {"text": "Club"}]}}}}}
DELETE_ACTION = {"markChatItemAsDeletedAction": {"targetItemId": "m0"}}


def test_parse_actions():
    up = parse_actions([TEXT_ACTION, PAID_ACTION, MEMBER_ACTION, DELETE_ACTION], time.time())
    assert [m.id for m in up.added] == ["m1", "p1", "j1"]
    m = up.added[0]
    assert m.kind == "text" and m.author == "@viewer" and m.is_member
    assert m.translatable_text == "こんにちは😀"
    assert m.plain_text.startswith("こんにちは") and ":_wave:" in m.plain_text
    assert m.custom_emoji[0].emoji_url == "https://yt3.ggpht.com/e1"
    assert up.added[1].kind == "paid" and up.added[1].amount == "¥500"
    assert up.added[2].header_text == "Welcome to Club"
    assert up.deleted == ["m0"]


def test_extract_page_data():
    html = ('<script>ytcfg.set({"INNERTUBE_API_KEY":"k","INNERTUBE_CLIENT_VERSION":"2.1","LOGGED_IN":false});'
            '</script><script>window["ytInitialData"] = {"contents":{"liveChatRenderer":{"continuations":'
            '[{"timedContinuationData":{"continuation":"TOP","timeoutMs":5000}}],"header":{"liveChatHeaderRenderer":'
            '{"viewSelector":{"sortFilterSubMenuRenderer":{"subMenuItems":[{"title":"Top chat","continuation":'
            '{"reloadContinuationData":{"continuation":"TOP"}}},{"title":"Live chat","continuation":'
            '{"reloadContinuationData":{"continuation":"ALL"}}}]}}}}}}};</script>')
    cfg = extract_ytcfg(html)
    data = extract_initial_data(html)
    page = ChatPage("vid", cfg, data)
    assert cfg["INNERTUBE_API_KEY"] == "k"
    assert page.continuation("live") == "ALL"
    assert page.continuation("top") == "TOP"


def test_sid_authorization_matches_ytdlp():
    from yt_dlp.extractor.youtube._base import YoutubeBaseInfoExtractor

    cookies = {"SAPISID": "abc", "__Secure-1PAPISID": "def", "__Secure-3PAPISID": "ghi"}
    ours = sid_authorization(cookies, "123", now=time.time())
    parts = {}
    for scheme, sid in (("SAPISIDHASH", "abc"), ("SAPISID1PHASH", "def"), ("SAPISID3PHASH", "ghi")):
        parts[scheme] = YoutubeBaseInfoExtractor._make_sid_authorization(scheme, sid, "https://www.youtube.com",
                                                                          {"u": "123"})
    assert ours == " ".join(parts.values())


def test_parse_metadata():
    from tsuyaku.chat.metadata import parse_metadata

    data = {
        "actions": [
            {"updateViewershipAction": {"viewCount": {"videoViewCountRenderer": {
                "viewCount": {"simpleText": "4,482 watching now"}, "originalViewCount": "4482"}}}},
            {"updateTitleAction": {"title": {"runs": [{"text": "【ライブ】"}, {"text": "天気"}]}}},
            {"updateDateTextAction": {"dateText": {"simpleText": "Started streaming 23 minutes ago"}}},
        ],
        "frameworkUpdates": {"entityBatchUpdate": {"mutations": [
            {"payload": {"likeCountEntity": {"likeCountIfIndifferentNumber": "214"}}}]}},
    }
    st = parse_metadata(data)
    assert st.viewers == 4482 and st.viewers_text == "4,482 watching now"
    assert st.likes == 214 and st.title == "【ライブ】天気"
    assert st.date_text.startswith("Started")
