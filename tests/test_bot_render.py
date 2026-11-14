from __future__ import annotations

from pathlib import Path

from media_butler import callbacks
from media_butler.bot import MediaButlerBot
from media_butler.db import Database


def make_bot(build_config, tmp_path: Path) -> tuple[MediaButlerBot, Database]:
    config = build_config()
    db = Database(tmp_path / "state.db")
    return MediaButlerBot(config, db), db


def add_item(db: Database, *, status: str = "pending", kind: str = "folder", **extra):
    return db.add_item(
        item_id=extra.pop("item_id", "a1b2c3d4"),
        source=extra.pop("source", "/inbox/Movie"),
        kind=kind,
        display_name=extra.pop("display_name", "Movie.2021.1080p"),
        size_bytes=extra.pop("size_bytes", 8 * 1024**3),
        video_count=extra.pop("video_count", 2),
        fingerprint=extra.pop("fingerprint", "fp"),
        status=status,
        **extra,
    )


def test_pending_prompt_has_three_destinations(build_config, tmp_path: Path):
    bot, db = make_bot(build_config, tmp_path)
    item = add_item(db)

    text = bot._prompt_text(item)
    keyboard = bot._prompt_keyboard(item)

    assert "Movie.2021.1080p" in text
    assert "2 videos" in text
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == 4
    destinations = [callbacks.parse_move(button.callback_data) for button in buttons[:3]]
    assert [destination for _, destination in destinations] == ["kids", "movies", "series"]
    assert callbacks.parse_ignore(buttons[3].callback_data) == item.id


def test_blocked_prompt_only_offers_ignore(build_config, tmp_path: Path):
    bot, db = make_bot(build_config, tmp_path)
    item = add_item(
        db,
        status="blocked",
        kind="blocked",
        error="the archive is password protected",
    )

    text = bot._prompt_text(item)
    keyboard = bot._prompt_keyboard(item)

    assert "password" in text
    assert len(keyboard.inline_keyboard) == 1
    retry, ignore = keyboard.inline_keyboard[0]
    assert callbacks.parse_retry(retry.callback_data) == item.id
    assert callbacks.parse_ignore(ignore.callback_data) == item.id


def test_single_video_prompt_does_not_say_videos(build_config, tmp_path: Path):
    bot, db = make_bot(build_config, tmp_path)
    item = add_item(db, kind="video", video_count=1, size_bytes=1024**3)

    assert "2 videos" not in bot._prompt_text(item)


def test_long_names_are_truncated(build_config, tmp_path: Path):
    bot, db = make_bot(build_config, tmp_path)
    item = add_item(db, display_name="x" * 1000)

    assert len(bot._prompt_text(item)) < 1000
