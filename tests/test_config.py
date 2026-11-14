from __future__ import annotations

from pathlib import Path

import pytest

from media_butler.config import ConfigError, _normalise_extensions, load_config


def test_loads_valid_config(build_config):
    config = load_config(build_config().source)
    assert config.paths.inbox.name == "inbox"
    assert config.telegram.chat_id == 111
    assert config.scan.video_extensions[0].startswith(".")
    assert config.emby.enabled is False
    assert config.extract_dir == config.paths.inbox / "_extracted"


def test_extensions_are_normalised_and_longest_first():
    values = _normalise_extensions([".ZIP", "rar", ".tar.gz", ".gz"])
    assert values.index(".tar.gz") < values.index(".gz")
    assert ".zip" in values and ".rar" in values


def test_archive_stem_strips_double_extension(build_config):
    config = build_config()
    assert config.scan.archive_stem("Movie.2021.tar.gz") == "Movie.2021"
    assert config.scan.archive_stem("Show.S01.zip") == "Show.S01"
    assert config.scan.is_archive("x.RAR") is True
    assert config.scan.is_video("x.MKV") is True


def test_negative_chat_id_for_groups(build_config):
    config = build_config(chat_id=-1001234567890)
    assert config.telegram.chat_id == -1001234567890


def test_nested_inbox_and_destination_is_rejected(tmp_path: Path):
    inbox = tmp_path / "inbox"
    destination = inbox / "Kids"
    destination.mkdir(parents=True)
    token = tmp_path / "token"
    token.write_text("x", encoding="utf-8")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
database = "{tmp_path / 'state.db'}"
[telegram]
token_file = "{token}"
allowed_users = [1]
[paths]
inbox = "{inbox}"
kids = "{destination}"
movies = "{tmp_path / 'm'}"
series = "{tmp_path / 's'}"
[scan]
interval_seconds = 30
settle_seconds = 0
[emby]
enabled = false
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="nested"):
        load_config(config_file)


def test_missing_token_is_an_error(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
database = "{tmp_path / 'state.db'}"
[telegram]
token_file = "{tmp_path / 'nope'}"
allowed_users = [1]
[paths]
inbox = "{tmp_path}"
kids = "{tmp_path}"
movies = "{tmp_path}"
series = "{tmp_path}"
[emby]
enabled = false
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_file, check_paths=False)
