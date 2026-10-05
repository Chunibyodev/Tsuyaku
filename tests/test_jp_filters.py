from tsuyaku import jp
from tsuyaku.asr import filters


def test_is_japanese():
    assert jp.is_japanese("こんにちは")
    assert jp.is_japanese("草")
    assert not jp.is_japanese("hello")
    assert not jp.is_japanese("안녕하세요 草")


def test_quick_chat_translation():
    assert jp.quick_chat_translation("草") == "lol"
    assert jp.quick_chat_translation("ｗｗｗｗｗ") == "LOL"
    assert jp.quick_chat_translation("8888") == "👏👏👏"
    assert jp.quick_chat_translation("おつ！") == "Good work!"
    assert jp.quick_chat_translation("今日の配信楽しみ") is None


def test_looks_chinese():
    assert jp.looks_chinese("生日快乐！")
    assert not jp.looks_chinese("今来た！何があったの？")
    assert not jp.looks_chinese("神回")


def test_collapse_and_repeat_cut():
    assert filters.collapse_self_repeat("打ち上げの成功率は高い方打ち上げの成功率は高い方") == "打ち上げの成功率は高い方"
    assert filters.collapse_self_repeat("今日はいい天気ですね") == "今日はいい天気ですね"
    text = "気象庁は雪や路面の凍結による交通への影響、暴風雪や高波に警戒するとともに、雪や路面の凍結による交通への影響、暴風雪や高"
    assert filters.cut_repeated_tail(text) == "気象庁は雪や路面の凍結による交通への影響、暴風雪や高波に警戒するとともに"


def test_clean_hallucinations():
    # Classic Whisper phantom on silence is dropped...
    assert filters.clean("ご視聴ありがとうございました", 1.0, no_speech_prob=0.7) is None
    # ...but kept when it was clearly said.
    assert filters.clean("ご視聴ありがとうございました", 3.0, no_speech_prob=0.01, avg_logprob=-0.1)
    assert filters.clean("あああああああああああああああああ", 1.0) is None
    assert filters.clean("   ", 1.0) is None
    assert filters.clean("今日はマイクラをやります", 2.0) == "今日はマイクラをやります"
