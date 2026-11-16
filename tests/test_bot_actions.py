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


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=1)


def start_update(user_id: int, chat_id: int, chat_type: str):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="someone"),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        message=message,
    )
    return update, message


@pytest.mark.asyncio
async def test_start_in_a_group_reveals_the_chat_id(build_config, tmp_path: Path):
    bot, _, _ = make_bot(build_config, tmp_path)
    update, message = start_update(999, -1001234567890, "supergroup")

    await bot.cmd_start(update, SimpleNamespace())

    assert message.replies
    assert "999" in message.replies[0]
    assert "-1001234567890" in message.replies[0]


@pytest.mark.asyncio
async def test_start_in_a_private_chat_does_not_show_a_chat_id(build_config, tmp_path: Path):
    bot, _, _ = make_bot(build_config, tmp_path)
    update, message = start_update(999, 999, "private")

    await bot.cmd_start(update, SimpleNamespace())

    assert "chat_id is" not in message.replies[0]


def scan_message():
    return SimpleNamespace(chat_id=111, message_id=42)


@pytest.mark.asyncio
async def test_manual_scan_reports_a_full_queue(build_config, tmp_path: Path):
    bot, db, fake = make_bot(build_config, tmp_path)
    for index in range(bot.config.scan.max_pending):
        add_pending(db, tmp_path / f"item{index}", item_id=f"{index:08x}")

    await bot._manual_scan(scan_message())

    assert "queue is full" in fake.edits[-1][2]


@pytest.mark.asyncio
async def test_manual_scan_explains_that_nothing_is_new(build_config, tmp_path: Path):
    bot, _, fake = make_bot(build_config, tmp_path)

    await bot._manual_scan(scan_message())

    assert "nothing new" in fake.edits[-1][2]
    assert "/scan again" in fake.edits[-1][2]


@pytest.mark.asyncio
async def test_manual_scan_reports_batches_already_waiting(build_config, tmp_path: Path):
    from media_butler.scanner import Scanner

    bot, db, fake = make_bot(build_config, tmp_path)
    folder = bot.config.paths.inbox / "Show.S01"
    folder.mkdir()
    (folder / "e01.mkv").write_text("data")
    fingerprint = Scanner(bot.config, db)._tree_signature(folder).token()
    add_pending(db, folder, fingerprint=fingerprint, display_name="Show.S01")

    await bot._manual_scan(scan_message())
    await bot._manual_scan(scan_message())

    assert "already" in fake.edits[-1][2]
    assert "1 batch(es) ready" in fake.edits[-1][2]
