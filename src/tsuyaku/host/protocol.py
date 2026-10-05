"""Firefox native messaging: every message is a 4-byte length (native byte order) + UTF-8 JSON.

stdout belongs to the protocol, so `claim_stdio()` moves everything else that would print there
(our own code, libraries, C extensions) over to stderr, which Firefox shows in its Browser Console.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
import threading
from typing import BinaryIO

log = logging.getLogger(__name__)

# Firefox drops messages from the app that are bigger than this.
MAX_TO_BROWSER = 1024 * 1024
_HEADER = struct.Struct("=I")


def _read_exact(stream: BinaryIO, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_message(stream: BinaryIO) -> dict | None:
    """The next message, or None when the browser closed the connection."""
    header = _read_exact(stream, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    data = _read_exact(stream, length)
    if data is None:
        return None
    msg = json.loads(data.decode("utf-8"))
    return msg if isinstance(msg, dict) else {}


def encode_message(msg: dict) -> bytes:
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_TO_BROWSER:
        raise ValueError(f"message too large for the browser ({len(data)} bytes)")
    return _HEADER.pack(len(data)) + data


class Writer:
    """Sends messages to the browser; safe to call from any thread."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self._lock = threading.Lock()
        self.closed = False

    def send(self, msg: dict) -> None:
        try:
            data = encode_message(msg)
        except ValueError as exc:
            log.warning("not sent: %s", exc)
            return
        with self._lock:
            if self.closed:
                return
            try:
                self.stream.write(data)
                self.stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closed = True  # the browser is gone; stdin reaches EOF next


def claim_stdio() -> tuple[BinaryIO, BinaryIO]:
    """(input, output) streams for the protocol. Afterwards fd 1 points at stderr, so a stray
    print() can never corrupt a message."""
    out_fd = os.dup(1)
    try:
        os.dup2(2, 1)
    except OSError:
        pass
    sys.stdout = sys.stderr
    in_fd = 0
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(in_fd, os.O_BINARY)
        msvcrt.setmode(out_fd, os.O_BINARY)
    return os.fdopen(in_fd, "rb", buffering=0, closefd=False), os.fdopen(out_fd, "wb")
