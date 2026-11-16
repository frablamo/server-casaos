from __future__ import annotations

import stat
from pathlib import Path

from media_butler.db import Database
from media_butler.scanner import Scanner, is_secondary_volume


def make_scanner(config, tmp_path: Path) -> tuple[Scanner, Database]:
    db = Database(tmp_path / "state.db")
    return Scanner(config, db), db


def test_secondary_rar_volumes_are_ignored():
    assert is_secondary_volume("Movie.part1.rar") is False
    assert is_secondary_volume("Movie.part2.rar") is True
    assert is_secondary_volume("Movie.r00") is True
    assert is_secondary_volume("Movie.rar") is False
    assert is_secondary_volume("Movie.mkv") is False


def test_folder_needs_two_scans_with_settle_zero(build_config, tmp_path: Path):
    config = build_config(settle_seconds=0)
    folder = config.paths.inbox / "Show.S01"
    folder.mkdir()
    (folder / "e01.mkv").write_text("data")

    scanner, _ = make_scanner(config, tmp_path)

    assert scanner.scan_once() == []
    batches = scanner.scan_once()

    assert len(batches) == 1
    batch = batches[0]
    assert batch.kind == "folder"
    assert batch.display_name == "Show.S01"
    assert batch.video_count == 1
    assert batch.size_bytes == 4


def test_partial_file_keeps_batch_unstable(build_config, tmp_path: Path):
    config = build_config(settle_seconds=0)
    folder = config.paths.inbox / "Movie"
    folder.mkdir()
    (folder / "Movie.mkv").write_text("data")
    (folder / "Movie.mkv.part").write_text("partial")

    scanner, _ = make_scanner(config, tmp_path)

    assert scanner.scan_once() == []
    assert scanner.scan_once() == []


def test_folder_without_videos_is_ignored(build_config, tmp_path: Path):
    config = build_config(settle_seconds=0)
    folder = config.paths.inbox / "Subtitles"
    folder.mkdir()
    (folder / "eng.srt").write_text("subs")

    scanner, _ = make_scanner(config, tmp_path)

    scanner.scan_once()
    assert scanner.scan_once() == []


def test_loose_video(build_config, tmp_path: Path):
    config = build_config(settle_seconds=0)
    video = config.paths.inbox / "Movie.2020.mkv"
    video.write_text("data")

    scanner, _ = make_scanner(config, tmp_path)

    scanner.scan_once()
    batches = scanner.scan_once()

    assert len(batches) == 1
    assert batches[0].kind == "video"
    assert batches[0].display_name == "Movie.2020.mkv"


def test_ignored_batch_is_not_asked_again(build_config, tmp_path: Path):
    config = build_config(settle_seconds=0)
    folder = config.paths.inbox / "Movie"
    folder.mkdir()
    (folder / "Movie.mkv").write_text("data")

    scanner, db = make_scanner(config, tmp_path)
    scanner.scan_once()
    batch = scanner.scan_once()[0]

    db.add_item(
        item_id="a1b2c3d4",
        source=str(batch.source),
        kind=batch.kind,
        display_name=batch.display_name,
        size_bytes=batch.size_bytes,
        video_count=batch.video_count,
        fingerprint=batch.fingerprint,
        status="ignored",
    )

    assert scanner.scan_once() == []


def _fake_seven_zip(tmp_path: Path) -> Path:
    script = tmp_path / "fake7z"
    script.write_text(
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args and args[0] == "l":
    print("Listing archive: fake")
    print()
    print("--")
    print("Path = fake.zip")
    print("Type = zip")
    print("Physical Size = 10")
    print()
    print("----------")
    print()
    print("Path = Show/e01.mkv")
    print("Size = 4")
    print("Attributes = A")
    print("Encrypted = -")
    sys.exit(0)

if args and args[0] == "x":
    target = next((arg[2:] for arg in args if arg.startswith("-o")), None)
    os.makedirs(os.path.join(target, "Show"), exist_ok=True)
    open(os.path.join(target, "Show", "e01.mkv"), "wb").write(b"data")
    sys.exit(0)

sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_archive_is_extracted_and_offered(build_config, tmp_path: Path):
    seven_zip = _fake_seven_zip(tmp_path)
    config = build_config(settle_seconds=0, seven_zip=str(seven_zip))
    source = config.paths.inbox / "Show.S01.zip"
    source.write_text("archive")

    scanner, _ = make_scanner(config, tmp_path)
    scanner.scan_once()
    batches = scanner.scan_once()

    assert len(batches) == 1
    batch = batches[0]
    assert batch.kind == "archive"
    assert batch.display_name == "Show.S01"
    assert batch.source.parent == config.extract_dir
    assert batch.archive_path == source
    assert (batch.source / "Show" / "e01.mkv").read_text() == "data"


def test_archive_without_videos_is_blocked(build_config, tmp_path: Path):
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
    print("Physical Size = 1")
    print()
    print("----------")
    print()
    print("Path = readme.txt")
    print("Size = 1")
    print("Attributes = A")
    sys.exit(0)
if args and args[0] == "x":
    target = next((arg[2:] for arg in args if arg.startswith("-o")), None)
    os.makedirs(target, exist_ok=True)
    open(os.path.join(target, "readme.txt"), "w").write("hi")
    sys.exit(0)
sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    config = build_config(settle_seconds=0, seven_zip=str(script))
    source = config.paths.inbox / "Docs.zip"
    source.write_text("archive")

    scanner, _ = make_scanner(config, tmp_path)
    scanner.scan_once()
    batches = scanner.scan_once()

    assert len(batches) == 1
    assert batches[0].kind == "blocked"
    assert "no videos" in (batches[0].problem or "")


def test_forced_scan_skips_the_settle_wait(build_config, tmp_path: Path):
    config = build_config(settle_seconds=120)
    folder = config.paths.inbox / "Show.S01"
    folder.mkdir()
    (folder / "e01.mkv").write_text("data")

    scanner, _ = make_scanner(config, tmp_path)

    assert scanner.scan_once() == []
    batches = scanner.scan_once(force={str(folder)})
    assert len(batches) == 1
    assert batches[0].source == folder


def test_forced_scan_still_refuses_partial_files(build_config, tmp_path: Path):
    config = build_config(settle_seconds=120)
    folder = config.paths.inbox / "Show.S01"
    folder.mkdir()
    (folder / "e01.mkv").write_text("data")
    (folder / "e02.mkv.part").write_text("downloading")

    scanner, _ = make_scanner(config, tmp_path)

    assert scanner.scan_once(force={str(folder)}) == []
