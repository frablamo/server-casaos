from __future__ import annotations

import shutil
from pathlib import Path

from media_butler.db import Database


def make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


def add(db: Database, item_id: str, source: str, *, status: str = "pending", **extra):
    return db.add_item(
        item_id=item_id,
        source=source,
        kind=extra.pop("kind", "folder"),
        display_name=extra.pop("display_name", Path(source).name),
        size_bytes=extra.pop("size_bytes", 10),
        video_count=extra.pop("video_count", 1),
        fingerprint=extra.pop("fingerprint", "fp"),
        status=status,
        **extra,
    )


def test_active_duplicate_is_rejected(tmp_path: Path):
    db = make_db(tmp_path)
    assert add(db, "a" * 8, "/inbox/x") is not None
    assert add(db, "b" * 8, "/inbox/x") is None
    db.set_status("a" * 8, "done")
    assert add(db, "b" * 8, "/inbox/x") is not None


def test_blocked_counts_as_active(tmp_path: Path):
    db = make_db(tmp_path)
    assert add(db, "a" * 8, "/inbox/x.zip", status="blocked") is not None
    assert add(db, "b" * 8, "/inbox/x.zip") is None


def test_ignored_fingerprint_is_skipped(tmp_path: Path):
    db = make_db(tmp_path)
    add(db, "a" * 8, "/inbox/x", status="ignored", fingerprint="one")
    assert db.should_skip("/inbox/x", "one") is True
    assert db.should_skip("/inbox/x", "two") is False


def test_archive_known(tmp_path: Path):
    db = make_db(tmp_path)
    add(db, "a" * 8, "/inbox/x.zip", status="done", fingerprint="one", archive_path="/inbox/x.zip")
    assert db.archive_known("/inbox/x.zip", "one") is True
    assert db.archive_known("/inbox/x.zip", "two") is False
    db.set_status("a" * 8, "pending")
    assert db.archive_known("/inbox/x.zip", "one") is True
    db.delete("a" * 8)
    assert db.archive_known("/inbox/x.zip", "one") is False


def test_counts_and_pending(tmp_path: Path):
    db = make_db(tmp_path)
    add(db, "a" * 8, "/inbox/a")
    add(db, "b" * 8, "/inbox/b")
    assert db.pending_count() == 2
    db.set_status("a" * 8, "done")
    assert db.pending_count() == 1


def test_awaiting_count_includes_blocked(tmp_path: Path):
    db = make_db(tmp_path)
    add(db, "a" * 8, "/inbox/a")
    add(db, "b" * 8, "/inbox/b", status="blocked")
    add(db, "c" * 8, "/inbox/c", status="done")
    assert db.awaiting_count() == 2


def test_reconcile_moving_done_when_destination_exists(tmp_path: Path):
    db = make_db(tmp_path)
    source = tmp_path / "inbox" / "movie"
    final = tmp_path / "Movies" / "movie"
    source.mkdir(parents=True)
    final.mkdir(parents=True)
    (source / "a.mkv").write_text("x")
    add(db, "a" * 8, str(source), status="moving")
    db.update("a" * 8, final_path=str(final))

    repaired = db.reconcile_moving(delete_source=shutil.rmtree)

    assert repaired["done"] == 1
    assert db.get("a" * 8).status == "done"
    assert not source.exists()


def test_reconcile_moving_back_to_pending(tmp_path: Path):
    db = make_db(tmp_path)
    source = tmp_path / "inbox" / "movie"
    source.mkdir(parents=True)
    (source / "a.mkv").write_text("x")
    add(db, "a" * 8, str(source), status="moving")
    db.update("a" * 8, final_path=str(tmp_path / "Movies" / "movie"))

    repaired = db.reconcile_moving()

    assert repaired["pending"] == 1
    assert db.get("a" * 8).status == "pending"
    assert db.get("a" * 8).final_path is None


def test_purge_old(tmp_path: Path):
    db = make_db(tmp_path)
    add(db, "a" * 8, "/inbox/a", status="done")
    add(db, "b" * 8, "/inbox/b", status="pending")
    assert db.purge_old(days=-1) == 1
    assert db.get("a" * 8) is None
    assert db.get("b" * 8) is not None


def test_state_roundtrip(tmp_path: Path):
    db = make_db(tmp_path)
    db.state_set("boot_notify", "1")
    assert db.state_get("boot_notify") == "1"
    db.state_delete("boot_notify")
    assert db.state_get("boot_notify") is None
