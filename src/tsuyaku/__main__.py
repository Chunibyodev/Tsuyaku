"""Command line entry point.

    tsuyaku setup                download models and helper programs
    tsuyaku run URL              headless: print live English subtitles in the terminal
    tsuyaku chat URL             headless: print translated live chat
    tsuyaku bench [--url URL | --audio FILE]   measure speech + translation speed
    tsuyaku doctor               show GPU / component status
    tsuyaku translate "text"     English -> Japanese like the message box
    tsuyaku firefox              connect the Firefox extension to this install
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import threading
import time

from . import __version__, logsetup


def _progress(done: int, total: int, label: str) -> None:
    if total:
        pct = done * 100 / total
        sys.stdout.write(f"\r  {label}: {done / 1e6:7.1f} / {total / 1e6:.1f} MB ({pct:5.1f}%)")
    else:
        sys.stdout.write(f"\r  {label}: {done / 1e6:7.1f} MB")
    sys.stdout.flush()


# ============================================================================ setup / doctor


def cmd_setup(args) -> int:
    from .config import Config
    from .setup import components

    cfg = Config.load()
    if args.engine:
        cfg.asr.engine = args.engine
        cfg.save()
    failed = False
    for comp in components(cfg):
        if comp.is_ready() and not args.force:
            print(f"✓ {comp.title} ({comp.description})")
            continue
        print(f"↓ {comp.title} ({comp.size_hint}) …")
        try:
            comp.install(_progress, None)
            print(f"\r✓ {comp.title} installed" + " " * 40)
        except Exception as exc:  # noqa: BLE001
            failed = failed or comp.required
            print(f"\r✗ {comp.title} failed: {exc}")
    return 1 if failed else 0


def cmd_doctor(args) -> int:
    import ctranslate2
    import yt_dlp

    from .asr.engines import cuda_device_count, pick_compute_type
    from .config import Config
    from .ingest.resolver import js_runtimes
    from .mt.server import gpu_info
    from .setup import components

    cfg = Config.load()
    print(f"Tsuyaku {__version__}  (Python {sys.version.split()[0]}, {sys.platform})")
    print(f"GPU: {gpu_info() or 'no NVIDIA GPU detected'}")
    n = cuda_device_count()
    print(f"CTranslate2 {ctranslate2.__version__}: {n} CUDA device(s)"
          + (f", compute type {pick_compute_type('cuda', cfg.asr.compute_type)}" if n else ""))
    print(f"yt-dlp {yt_dlp.version.__version__}, JS runtimes: {', '.join(js_runtimes())}")
    print(f"Speech engine: {cfg.asr.engine}   LLM: {cfg.llm.model_file if cfg.llm.manage_server else cfg.llm.base_url}")
    for comp in components(cfg):
        mark = "✓" if comp.is_ready() else ("✗" if comp.required else "-")
        print(f"  {mark} {comp.title}: {comp.description}")
    from . import paths
    from .host.install import registered_manifest

    manifest = registered_manifest()
    print(f"Firefox extension: {'connected (' + str(manifest) + ')' if manifest else 'not connected (run: tsuyaku firefox)'}")
    print(f"Config: {paths.config_file()}\nData:   {paths.data_dir()}\nLogs:   {paths.log_dir()}")
    return 0


# ============================================================================ headless run


class ConsoleListener:
    def __init__(self, show_chat: bool = True, show_subs: bool = True) -> None:
        self.show_chat = show_chat
        self.show_subs = show_subs
        self.session = None
        self.ended = asyncio.Event()
        self.chat_msgs: dict = {}
        self.printed: set[int] = set()

    def on_status(self, text, level="info"):
        print(f"[{level}] {text}", flush=True)

    def on_stream_info(self, info):
        print(f"▶ {info.title} — {info.channel} ({info.live_status})", flush=True)

    def on_cue(self, cue):
        if not self.show_subs or cue.id in self.printed:
            return
        if cue.status in ("done", "error"):
            self.printed.add(cue.id)
            lat = cue.pipeline_seconds
            first = cue.first_word_seconds
            timing = (f"asr {cue.asr_seconds * 1000:4.0f}ms  mt {(cue.mt_done_at - cue.mt_started_at) * 1000:4.0f}ms"
                      if cue.mt_started_at else f"asr {cue.asr_seconds * 1000:4.0f}ms")
            lat_s = f" | first word {first:.2f}s, done {lat:.2f}s after audio" if lat and first else ""
            print(f"\n[{cue.start:8.2f}] {cue.jp}\n           → {cue.display_text}\n           ({timing}{lat_s})",
                  flush=True)

    def on_chat(self, update):
        for m in update.added:
            self.chat_msgs[m.id] = m
            if self.session and self.session.chat_tr:
                self.session.chat_tr.submit(m.id, m.translatable_text)

    def on_chat_translation(self, msg_id, text):
        m = self.chat_msgs.pop(msg_id, None)
        if not m or not self.show_chat:
            return
        from .mt.translator import SKIPPED

        en = "" if text in (None, SKIPPED) else f"  →  {text}"
        amount = f" [{m.amount}]" if m.amount else ""
        print(f"  💬 {m.author}{amount}: {m.plain_text}{en}", flush=True)

    def on_stats(self, stats):
        if stats.viewers_text and stats.viewers_text != getattr(self, "_viewers", None):
            self._viewers = stats.viewers_text
            likes = f", {stats.likes:,} likes" if stats.likes is not None else ""
            print(f"[stats] {stats.viewers_text}{likes}", flush=True)

    def on_ended(self, reason):
        print(f"■ {reason}", flush=True)
        self.ended.set()


async def _headless(url: str, seconds: float | None, show_chat: bool, show_subs: bool) -> int:
    from .config import Config
    from .engine import Engine
    from .ingest.resolver import ResolveError
    from .session import Session

    cfg = Config.load()
    cfg.chat.enabled = show_chat
    engine = Engine(cfg)
    if show_subs:
        engine.start_background()
    else:
        threading.Thread(target=engine.start_llm, daemon=True).start()
    listener = ConsoleListener(show_chat=show_chat, show_subs=show_subs)
    session = Session(engine, url, listener, subtitles=show_subs, realtime_files=True)
    listener.session = session
    try:
        await session.start()
    except ResolveError as exc:
        print(f"Could not open stream: {exc}")
        engine.shutdown()
        return 1
    t0 = time.monotonic()
    idle_since = None
    try:
        while not listener.ended.is_set():
            await asyncio.sleep(0.5)
            if seconds and time.monotonic() - t0 > seconds:
                break
            if session.local_file:
                if session.idle():
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 2.0:
                        break
                else:
                    idle_since = None
    except KeyboardInterrupt:
        pass
    finally:
        stats = session.stats()
        await session.stop()
        await engine.aclose()
        engine.shutdown()
    if stats:
        print("\nAverages: " + ", ".join(f"{k} {v:.2f}" for k, v in stats.items() if isinstance(v, float)))
    return 0


def cmd_run(args) -> int:
    return asyncio.run(_headless(args.url, args.seconds, show_chat=args.chat, show_subs=True))


def cmd_chat(args) -> int:
    return asyncio.run(_headless(args.url, args.seconds, show_chat=True, show_subs=False))


# ============================================================================ bench


def _collect_audio(args):
    import numpy as np

    from .ingest.audio import FileAudioReader

    chunks = []
    if args.audio:
        import threading

        done = threading.Event()
        reader = FileAudioReader(args.audio, chunks.append, on_done=done.set)
        reader.start()
        done.wait()
    elif args.url:
        from .config import Config
        from .cookies import CookieSource
        from .ingest.live import LiveIngest
        from .ingest.resolver import resolve

        cfg = Config.load()
        info = resolve(args.url, max_height=360, cookies=CookieSource.from_config(cfg.account))
        print(f"Recording {args.seconds:.0f}s of audio from: {info.title}")
        if info.is_live:
            async def record():
                ingest = LiveIngest(info, chunks.append)
                task = asyncio.create_task(ingest.run())
                t0 = time.monotonic()
                while time.monotonic() - t0 < args.seconds:
                    await asyncio.sleep(0.5)
                    got = sum(len(c.samples) for c in chunks) / 16000
                    sys.stdout.write(f"\r  {got:5.1f}s recorded")
                    sys.stdout.flush()
                task.cancel()
                await ingest.close()
                print()

            asyncio.run(record())
        else:
            import threading

            done = threading.Event()
            reader = FileAudioReader(info.audio.url, chunks.append, headers=info.http_headers, on_done=done.set,
                                     max_ahead=lambda t: False)
            reader.start()
            while not done.is_set() and sum(len(c.samples) for c in chunks) / 16000 < args.seconds:
                time.sleep(0.2)
            reader.stop()
    else:
        raise SystemExit("bench needs --audio FILE or --url URL")
    if not chunks:
        raise SystemExit("No audio was captured.")
    audio = np.concatenate([c.samples for c in chunks])[: int(args.seconds * 16000)]
    return chunks[0].start, audio


def cmd_bench(args) -> int:
    import numpy as np

    from .asr.engines import create_engine
    from .asr.filters import clean
    from .asr.vad import Segmenter
    from .config import Config
    from .ingest.audio import AudioChunk

    cfg = Config.load()
    start, audio = _collect_audio(args)
    print(f"Audio: {len(audio) / 16000:.1f}s")
    seg = Segmenter(cfg.vad)
    utts = []
    for i in range(0, len(audio), 1600):
        utts += seg.feed(AudioChunk(start + i / 16000, audio[i:i + 1600]))
    utts += seg.flush()
    print(f"Voice detection: {len(utts)} phrases (max {cfg.vad.max_segment_s}s, silence {cfg.vad.min_silence_ms}ms)\n")

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    windows = [int(w) for w in args.windows.split(",")] if args.windows else [cfg.asr.window_s]
    results = {}
    lines_for_mt = []
    for name in engines:
        for window in (windows if name == "whisper" else [0]):
            acfg = Config.load().asr
            acfg.engine = name
            if window:
                acfg.window_s = window
            label = f"{name}" + (f" (window {window}s)" if window else "")
            try:
                eng = create_engine(acfg)
            except Exception as exc:  # noqa: BLE001
                print(f"✗ {label}: {exc}\n")
                continue
            if utts:
                eng.transcribe(utts[0].audio)  # warm-up
            times, texts = [], []
            for u in utts:
                r = eng.transcribe(u.audio)
                times.append(r.elapsed)
                text = clean(r.text, u.duration, avg_logprob=r.avg_logprob, no_speech_prob=r.no_speech_prob,
                             speech_ratio=u.speech_ratio) or ""
                texts.append(text)
            results[label] = (times, texts, eng.device)
            if not lines_for_mt:
                lines_for_mt = [t for t in texts if t]
            p90 = float(np.percentile(times, 90)) if times else 0
            print(f"■ {label} on {eng.device}: mean {statistics.mean(times) * 1000:.0f} ms, "
                  f"p90 {p90 * 1000:.0f} ms per phrase")
            del eng

    if len(results) > 0:
        print("\nTranscripts (compare accuracy):")
        labels = list(results)
        for i, u in enumerate(utts):
            print(f"  [{u.start - start:6.1f}s]")
            for label in labels:
                print(f"     {label[:22]:22} {results[label][1][i]}")

    if args.llm and lines_for_mt:
        print("\nTranslation speed:")
        asyncio.run(_bench_llm(cfg, lines_for_mt[: args.lines]))
    return 0


async def _bench_llm(cfg, lines):
    from .cues import Cue, CueStore
    from .engine import Engine
    from .mt.translator import StreamMeta, SubtitleTranslator

    engine = Engine(cfg)
    await asyncio.to_thread(engine.start_llm)
    if engine.llm is None:
        print(f"  ✗ translator unavailable: {engine.llm_error}")
        return
    store = CueStore()
    tr = SubtitleTranslator(engine.llm, store, context_lines=cfg.subtitles.context_lines)
    tr.set_context(StreamMeta("benchmark"), engine.glossary.entries_for(None))
    firsts, totals = [], []
    for i, line in enumerate(lines):
        cue = store.add(Cue(start=i, end=i + 1, jp=line))
        await tr.translate(cue)
        if i > 0:  # first request includes the system prompt (cold cache)
            firsts.append(cue.mt_first_at - cue.mt_started_at)
            totals.append(cue.mt_done_at - cue.mt_started_at)
        print(f"  {line}\n    → {cue.en}  ({(cue.mt_done_at - cue.mt_started_at) * 1000:.0f} ms)")
    if totals:
        print(f"■ LLM: first word {statistics.mean(firsts) * 1000:.0f} ms, full line "
              f"{statistics.mean(totals) * 1000:.0f} ms (mean, warm cache)")
    await engine.aclose()
    engine.shutdown()


# ============================================================================ message box


def cmd_translate(args) -> int:
    async def run():
        from .config import Config
        from .engine import Engine

        cfg = Config.load()
        engine = Engine(cfg)
        await asyncio.to_thread(engine.start_llm)  # the local model (also the cloud model's fallback)
        if not engine.composer.available:
            print(f"Translator unavailable: {engine.llm_error}")
            return 1
        try:
            result = await engine.composer.compose(args.text, args.tone, versions=cfg.compose.versions)
        finally:
            await engine.aclose()
            engine.shutdown()
        for i, v in enumerate(result.versions, 1):
            print(f"{i}. JP:   {v.ja}\n   Back: {v.back}")
        print(f"({result.source}{'; ' + result.note if result.note else ''})")
        return 0

    return asyncio.run(run())


# ============================================================================ Firefox extension


def cmd_firefox(args) -> int:
    from .host import install

    if args.remove:
        install.unregister()
        print("Removed the Firefox connection (the extension can no longer start Tsuyaku).")
        return 0
    try:
        manifest = install.register()
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ Firefox can now start Tsuyaku (native host: {manifest})")
    xpi = install.build_xpi()
    folder = install.extension_dir()
    print(f"✓ Extension package: {xpi}\n")
    print("Add the extension to Firefox (see docs/FIREFOX.md):")
    print("  • Try it now: open about:debugging#/runtime/this-firefox → \"Load Temporary Add-on…\" →")
    print(f"    {folder / 'manifest.json'}   (stays until Firefox restarts)")
    print("  • Keep it: sign the .xpi for free on addons.mozilla.org (unlisted), then open it in Firefox,")
    print("    or use Firefox Developer Edition/Nightly with xpinstall.signatures.required = false.")
    return 0


def _utf8_console() -> None:
    """Windows consoles default to a legacy code page that can't print ✓/✗/Japanese."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> None:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="tsuyaku", description="Live Japanese→English YouTube client")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("setup", help="download models and helper programs")
    p.add_argument("--engine", choices=["whisper", "reazon"], help="speech engine to set up")
    p.add_argument("--force", action="store_true", help="re-download even if present")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="show GPU and component status")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="print live English subtitles in the terminal")
    p.add_argument("url", help="YouTube URL/ID, @channel, .m3u8, or a local media file")
    p.add_argument("--seconds", type=float, default=None, help="stop after N seconds")
    p.add_argument("--chat", action="store_true", help="also print translated chat")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("chat", help="print translated live chat")
    p.add_argument("url")
    p.add_argument("--seconds", type=float, default=None)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("bench", help="measure speech recognition and translation speed")
    p.add_argument("--url", help="record audio from this stream")
    p.add_argument("--audio", help="or use this audio/video file")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--engines", default="whisper,reazon", help="comma list: whisper,reazon")
    p.add_argument("--windows", default="6,30", help="Whisper windows to compare, e.g. 6,10,30")
    p.add_argument("--lines", type=int, default=12, help="lines to translate in the LLM test")
    p.add_argument("--no-llm", dest="llm", action="store_false")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("translate", help="English → Japanese like the message box")
    p.add_argument("text")
    p.add_argument("--tone", choices=["casual", "polite", "fan"], default="casual")
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("firefox", help="connect the Firefox extension to this install")
    p.add_argument("--remove", action="store_true", help="remove the connection again")
    p.set_defaults(func=cmd_firefox)

    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        print("\nTsuyaku runs in Firefox: see docs/FIREFOX.md. `tsuyaku firefox` connects it.")
        sys.exit(0)
    logsetup.setup(verbose=args.verbose, console=True)
    try:
        sys.exit(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
