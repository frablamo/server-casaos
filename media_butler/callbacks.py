"""Encoding and parsing of inline-keyboard callback payloads.

Telegram limits ``callback_data`` to 64 bytes, so payloads stay tiny and every
value received back is validated before use.
"""

from __future__ import annotations

import re

DESTINATIONS = ("kids", "movies", "series")
DESTINATION_LABELS = {
    "kids": "👶 Kids",
    "movies": "🎥 Movies",
    "series": "📺 Series",
}

ITEM_ID_RE = re.compile(r"^[0-9a-f]{8}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{8}$")
MOVE_RE = re.compile(r"^mv:([0-9a-f]{8}):(kids|movies|series)$")
IGNORE_RE = re.compile(r"^ig:([0-9a-f]{8})$")
RETRY_RE = re.compile(r"^ry:([0-9a-f]{8})$")
REBOOT_RE = re.compile(r"^rt:([0-9a-f]{8}):(yes|no)$")

REBOOT_ACTIONS = ("yes", "no")


def encode_move(item_id: str, destination: str) -> str:
    if destination not in DESTINATIONS:
        raise ValueError(f"invalid destination: {destination}")
    if not ITEM_ID_RE.match(item_id):
        raise ValueError(f"invalid id: {item_id}")
    return f"mv:{item_id}:{destination}"


def encode_ignore(item_id: str) -> str:
    if not ITEM_ID_RE.match(item_id):
        raise ValueError(f"invalid id: {item_id}")
    return f"ig:{item_id}"


def encode_retry(item_id: str) -> str:
    if not ITEM_ID_RE.match(item_id):
        raise ValueError(f"invalid id: {item_id}")
    return f"ry:{item_id}"


def encode_reboot(token: str, action: str) -> str:
    if action not in REBOOT_ACTIONS:
        raise ValueError(f"invalid action: {action}")
    if not TOKEN_RE.match(token):
        raise ValueError(f"invalid token: {token}")
    return f"rt:{token}:{action}"


def parse_move(data: str) -> tuple[str, str] | None:
    match = MOVE_RE.match(data)
    return (match.group(1), match.group(2)) if match else None


def parse_ignore(data: str) -> str | None:
    match = IGNORE_RE.match(data)
    return match.group(1) if match else None


def parse_retry(data: str) -> str | None:
    match = RETRY_RE.match(data)
    return match.group(1) if match else None


def parse_reboot(data: str) -> tuple[str, str] | None:
    match = REBOOT_RE.match(data)
    return (match.group(1), match.group(2)) if match else None
