from __future__ import annotations

import stat
from pathlib import Path

from media_butler import organizer
from media_butler.bot import MediaButlerBot
from media_butler.db import Database
from media_butler.scanner import Scanner


def fake_seven_zip(tmp_path: Path) -> Path:
    script = tmp_path / "fake7z"
    script.write_text(
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args and args[0] == "l":
    print("--")
    print("Path = x.zip")
    print("Type = zip")
    print("Physical Size = 10")
    print()
    print("----------")
    print()
    print("Path = Movie.mkv")
    print("Size = 4")
    print("Attributes = A")
    print("Encrypted = -")
    sys.exit(0)
if args and args[0] == "x":
    target = next((arg[2:] for arg in args if arg.startswith("-o")), None)
    os.makedirs(target, exist_ok=True)
    open(os.path.join(target, "Movie.mkv"), "wb").write(b"data")
    sys.exit(0)
sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_full_archive_flow(build_config, tmp_path: Path):
    config = build_config(settle_seconds=0, seven_zip=str(fake_seven_zip(tmp_path)))
    db = Database(tmp_path / "state.db")
    bot = MediaButlerBot(config, db)
    scanner = Scanner(config, db)

    archive_path = config.paths.inbox / "Movie.2021.zip"
    archive_path.write_text("archive")

    scanner.scan_once()
    batch = scanner.scan_once()[0]
    item = db.add_item(
        item_id="a1b2c3d4",
        source=str(batch.source),
        kind=batch.kind,
        display_name=batch.display_name,
        size_bytes=batch.size_bytes,
        video_count=batch.video_count,
        fingerprint=batch.fingerprint,
        archive_path=str(archive_path),
    )
    assert item is not None

    final = organizer.move_to_destination(
        Path(item.source), config.paths.movies, item.display_name, item.id
    )
    db.set_status(item.id, "done", dest="movies", final_path=str(final))

    bot._cleanup_archive(item)

    assert (final / "Movie.mkv").read_text() == "data"
    assert not archive_path.exists()
    assert not Path(item.source).exists()
    assert list(config.extract_dir.glob("*.done")) == []
    # A second scan does not re-offer the deleted archive.
    assert scanner.scan_once() == []


def test_delete_archive_false_keeps_the_compressed_file(build_config, tmp_path: Path):
    config = build_config(
        settle_seconds=0,
        seven_zip=str(fake_seven_zip(tmp_path)),
        delete_archive=False,
    )
    db = Database(tmp_path / "state.db")
    bot = MediaButlerBot(config, db)
    scanner = Scanner(config, db)

    archive_path = config.paths.inbox / "Movie.2021.zip"
    archive_path.write_text("archive")
    scanner.scan_once()
    batch = scanner.scan_once()[0]
    item = db.add_item(
        item_id="a1b2c3d4",
        source=str(batch.source),
        kind=batch.kind,
        display_name=batch.display_name,
        size_bytes=batch.size_bytes,
        video_count=batch.video_count,
        fingerprint=batch.fingerprint,
        archive_path=str(archive_path),
    )

    final = organizer.move_to_destination(
        Path(item.source), config.paths.movies, item.display_name, item.id
    )
    db.set_status(item.id, "done", dest="movies", final_path=str(final))

    bot._cleanup_archive(item)
    assert archive_path.exists()
