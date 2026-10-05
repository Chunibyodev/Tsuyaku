import numpy as np

from tsuyaku.asr.vad import FRAME, Segmenter
from tsuyaku.config import VADConfig
from tsuyaku.ingest.audio import AudioChunk

SR = 16000


class EnergyVAD:
    """Deterministic stand-in for Silero: 'speech' = loud frame."""

    def __call__(self, frame):
        return 0.95 if float(np.sqrt(np.mean(frame**2))) > 0.05 else 0.02

    def reset(self):
        pass


def tone(seconds):
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype=np.float32)


def run(audio, cfg):
    seg = Segmenter(cfg, EnergyVAD())
    out = []
    for i in range(0, len(audio), 1600):
        out += seg.feed(AudioChunk(10.0 + i / SR, audio[i:i + 1600]))
    out += seg.flush()
    return out


def test_phrases_split_on_silence():
    audio = np.concatenate([silence(0.5), tone(1.0), silence(0.6), tone(1.2), silence(0.6)])
    utts = run(audio, VADConfig(min_silence_ms=300, max_segment_s=3.0, pad_ms=0))
    assert len(utts) == 2
    assert abs(utts[0].start - 10.5) < 0.1
    assert abs(utts[0].duration - 1.0) < 0.15
    assert not utts[0].forced_cut


def test_long_speech_is_force_cut():
    # 7 s of speech with a short dip in the middle; cap at 3 s.
    audio = np.concatenate([tone(2.2), silence(0.1), tone(4.7), silence(0.6)])
    utts = run(audio, VADConfig(min_silence_ms=300, max_segment_s=3.0, pad_ms=0))
    assert len(utts) >= 3
    assert all(u.duration <= 3.0 + FRAME / SR + 1e-6 for u in utts)
    assert utts[0].forced_cut
    # The quiet dip at ~2.2 s is preferred as the first cut point.
    assert abs(utts[0].end - (10.0 + 2.2)) < 0.25


def test_short_blips_are_dropped():
    audio = np.concatenate([silence(0.3), tone(0.1), silence(0.8)])
    assert run(audio, VADConfig(min_segment_s=0.25)) == []


def test_discontinuity_flushes():
    cfg = VADConfig(min_silence_ms=300, pad_ms=0)
    seg = Segmenter(cfg, EnergyVAD())
    a = tone(1.0)
    out = seg.feed(AudioChunk(0.0, a))
    out += seg.feed(AudioChunk(50.0, a))  # jump in time: previous phrase must be closed
    assert len(out) == 1 and out[0].start == 0.0
