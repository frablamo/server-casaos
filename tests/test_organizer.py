from __future__ import annotations

from pathlib import Path

import pytest

from media_butler import organizer
from media_butler.util import human_duration, human_size, safe_component


def test_safe_component():
    assert safe_component("  movie.  ") == "movie"
    assert safe_component("a/b\\c") == "a_b_c"
    assert safe_component("") == "unnamed"
    assert len(safe_component("x" * 500)) == 120


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(8 * 1024**3) == "8.0 GB"


def test_human_duration():
    assert human_duration(5) == "5s"
    assert human_duration(65) == "1m 5s"
    assert human_duration(3700) == "1h 1m"
    assert human_duration(90000) == "1d 1h"


def test_destination_path_avoids_collisions(tmp_path: Path):
    root = tmp_path / "Movies"
    root.mkdir()
    assert organizer.destination_path(root, "Dune") == root / "Dune"
    (root / "Dune").mkdir()
    assert organizer.destination_path(root, "Dune") == root / "Dune (1)"
    (root / "Dune (1)").mkdir()
    assert organizer.destination_path(root, "Dune") == root / "Dune (2)"


def test_destination_path_keeps_extension(tmp_path: Path):
    root = tmp_path / "Movies"
    root.mkdir()
    (root / "movie.mkv").write_text("x")
    assert organizer.destination_path(root, "movie.mkv").name == "movie (1).mkv"


def test_move_file(tmp_path: Path):
    source = tmp_path / "inbox" / "movie.mkv"
    source.parent.mkdir()
    source.write_text("data")
    root = tmp_path / "Movies"
    root.mkdir()

    final = organizer.move_to_destination(source, root, "movie.mkv", "a" * 8)

    assert final.read_text() == "data"
    assert not source.exists()


def test_move_directory(tmp_path: Path):
    source = tmp_path / "inbox" / "Show"
    (source / "Season 1").mkdir(parents=True)
    (source / "Season 1" / "e01.mkv").write_text("data")
    root = tmp_path / "Series"
    root.mkdir()

    final = organizer.move_to_destination(source, root, "Show", "a" * 8)

    assert (final / "Season 1" / "e01.mkv").read_text() == "data"
    assert not source.exists()


def test_dry_run_does_not_move(tmp_path: Path):
    source = tmp_path / "inbox" / "movie.mkv"
    source.parent.mkdir()
    source.write_text("data")
    root = tmp_path / "Movies"
    root.mkdir()

    intended = organizer.move_to_destination(source, root, "movie.mkv", "a" * 8, dry_run=True)

    assert intended == root / "movie.mkv"
    assert not intended.exists()
    assert source.exists()


def test_move_missing_source(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        organizer.move_path(tmp_path / "nope", tmp_path / "dst", "a" * 8)


def test_cleanup_temp_dirs(tmp_path: Path):
    (tmp_path / ".mb-tmp-abc").mkdir()
    (tmp_path / "keep").mkdir()
    assert organizer.cleanup_temp_dirs(tmp_path) == 1
    assert not (tmp_path / ".mb-tmp-abc").exists()
    assert (tmp_path / "keep").exists()
