from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from media_butler import organizer
from media_butler.bot import MediaButlerBot
from media_butler.db import Database


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=100 + len(self.sent), chat_id=chat_id)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text))


def make_bot(build_config, tmp_path: Path) -> tuple[MediaButlerBot, Database, FakeBot]:
    config = build_config()
    db = Database(tmp_path / "state.db")
    bot = MediaButlerBot(config, db)
    fake = FakeBot()
    bot.application = SimpleNamespace(bot=fake)
    return bot, db, fake


def query(message_id: int = 5):
    return SimpleNamespace(message=SimpleNamespace(chat_id=111, message_id=message_id))


def add_pending(db: Database, source: Path, **extra):
    return db.add_item(
        item_id=extra.pop("item_id", "a1b2c3d4"),
        source=str(source),
        kind=extra.pop("kind", "folder"),
        display_name=extra.pop("display_name", source.name),
        size_bytes=extra.pop("size_bytes", 10),
        video_count=extra.pop("video_count", 1),
        fingerprint=extra.pop("fingerprint", "fp"),
        status=extra.pop("status", "pending"),
        **extra,
    )


@pytest.mark.asyncio
async def test_handle_move_moves_and_marks_done(build_config, tmp_path: Path):
    bot, db, fake = make_bot(build_config, tmp_path)
    source = bot.config.paths.inbox / "Movie.2021"
    source.mkdir()
    (source / "movie.mkv").write_text("data")
    item = add_pending(db, source)

    await bot._handle_move(query(), item.id, "movies")

    stored = db.get(item.id)
    assert stored.status == "done"
    assert stored.dest == "movies"
    assert (Path(stored.final_path) / "movie.mkv").read_text() == "data"
    assert not source.exists()
    assert "✅" in fake.edits[-1][2]
    # The intermediate "moving" edit was also sent.
    assert any("Moving" in text for _, _, text in fake.edits)


@pytest.mark.asyncio
async def test_final_path_is_saved_before_the_move(build_config, tmp_path: Path, monkeypatch):
    bot, db, fake = make_bot(build_config, tmp_path)
    source = bot.config.paths.inbox / "Movie.2021"
    source.mkdir()
    (source / "movie.mkv").write_text("data")
    item = add_pending(db, source)

    seen: dict[str, object] = {}
    real_move = organizer.move_to_destination

    def spy(*args, **kwargs):
        stored = db.get(item.id)
        seen["status"] = stored.status
        seen["final_path"] = stored.final_path
        return real_move(*args, **kwargs)

    monkeypatch.setattr(organizer, "move_to_destination", spy)

    await bot._handle_move(query(), item.id, "movies")

    planned = bot.config.paths.movies / "Movie.2021"
    assert seen == {"status": "moving", "final_path": str(planned)}
    assert db.get(item.id).status == "done"


@pytest.mark.asyncio
async def test_handle_move_rejects_already_done(build_config, tmp_path: Path):
    bot, db, fake = make_bot(build_config, tmp_path)
    source = bot.config.paths.inbox / "Movie"
    source.mkdir()
    item = add_pending(db, source, status="done")

    await bot._handle_move(query(), item.id, "kids")

    assert source.exists()
    assert "no longer pending" in fake.edits[-1][2]


@pytest.mark.asyncio
async def test_handle_move_reports_failure_and_keeps_pending(build_config, tmp_path: Path):
    bot, db, fake = make_bot(build_config, tmp_path)
    source = bot.config.paths.inbox / "Missing"
    item = add_pending(db, source)

    await bot._handle_move(query(), item.id, "series")

    stored = db.get(item.id)
    assert stored.status == "pending"
    assert "Error" in fake.edits[-1][2]


@pytest.mark.asyncio
async def test_handle_ignore(build_config, tmp_path: Path):
    bot, db, fake = make_bot(build_config, tmp_path)
    source = bot.config.paths.inbox / "Nope"
    source.mkdir()
    item = add_pending(db, source)

    await bot._handle_ignore(query(), item.id)

    assert db.get(item.id).status == "ignored"
    assert source.exists()
    assert "ignored" in fake.edits[-1][2]


@pytest.mark.asyncio
async def test_handle_retry_deletes_blocked_item(build_config, tmp_path: Path, monkeypatch):
    bot, db, fake = make_bot(build_config, tmp_path)
    archive = bot.config.paths.inbox / "Broken.zip"
    archive.write_text("x")
    item = add_pending(db, archive, status="blocked", kind="blocked")

    async def fake_retry_scan(chat_id, message_id, name, source=None):
        fake.edits.append((chat_id, message_id, "retry-scan"))

    monkeypatch.setattr(bot, "_retry_scan", fake_retry_scan)

    await bot._handle_retry(query(9), item.id)

    assert db.get(item.id) is None
    assert "Retrying" in fake.edits[-1][2]
    # Let the spawned task run.
    for task in list(bot._tasks):
        await task
    assert any(text == "retry-scan" for _, _, text in fake.edits)
