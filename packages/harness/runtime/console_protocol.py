"""Framing for messages sent to persistent console-agent stdin pipes."""

from __future__ import annotations

import json


_PREFIX = "\x1eMAI1 "


def encode_console_message(text: str) -> bytes:
    """Encode one logical message as one newline-delimited transport frame."""
    if not isinstance(text, str):
        raise TypeError("console message must be a string")
    payload = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":"))
    return (_PREFIX + payload + "\n").encode("utf-8")


def decode_console_message(line: str) -> str:
    """Decode a framed message, while accepting legacy plain terminal lines."""
    line = line.rstrip("\r\n")
    if not line.startswith(_PREFIX):
        return line
    payload = json.loads(line[len(_PREFIX):])
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ValueError("invalid console message frame")
    return payload["text"]
