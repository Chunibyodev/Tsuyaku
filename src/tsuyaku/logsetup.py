"""Logging to a rotating file, and warnings to stderr (the console, or Firefox's Browser Console)."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from . import paths


def setup(verbose: bool = False, console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    try:
        fh = logging.handlers.RotatingFileHandler(
            paths.log_dir() / "tsuyaku.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)
    except OSError:
        pass
    if console and sys.stderr is not None:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "faster_whisper", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
