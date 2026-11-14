from __future__ import annotations

import json
from pathlib import Path

import pytest

from media_butler.config import load_config


@pytest.fixture
def build_config(tmp_path: Path):
    """Return a factory that writes a valid config.toml and loads it."""

    def factory(
        *,
        settle_seconds: int = 0,
        allowed_users: tuple[int, ...] = (111,),
        chat_id: int = 0,
        dry_run: bool = False,
        delete_archive: bool = True,
        archive_extensions: tuple[str, ...] | None = None,
        seven_zip: str = "7z",
        extra_scan: str = "",
    ):
        inbox = tmp_path / "inbox"
        kids = tmp_path / "Kids"
        movies = tmp_path / "Movies"
        series = tmp_path / "Series"
        for directory in (inbox, kids, movies, series):
            directory.mkdir(parents=True, exist_ok=True)

        token_file = tmp_path / "token"
        token_file.write_text("123456:TEST", encoding="utf-8")

        users = ", ".join(str(user) for user in allowed_users)
        archive_line = ""
        if archive_extensions:
            archive_line = "archive_extensions = [" + ", ".join(
                json.dumps(ext) for ext in archive_extensions
            ) + "]"

        text = f"""
database = {json.dumps(str(tmp_path / "state.db"))}

[telegram]
token_file = {json.dumps(str(token_file))}
allowed_users = [{users}]
chat_id = {chat_id}

[paths]
inbox = {json.dumps(str(inbox))}
kids = {json.dumps(str(kids))}
movies = {json.dumps(str(movies))}
series = {json.dumps(str(series))}

[scan]
interval_seconds = 30
settle_seconds = {settle_seconds}
max_pending = 10
seven_zip = {json.dumps(seven_zip)}
{archive_line}
{extra_scan}

[behavior]
delete_archive = {"true" if delete_archive else "false"}
dry_run = {"true" if dry_run else "false"}

[emby]
enabled = false
url = ""
api_key = ""

[logging]
level = "DEBUG"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(text, encoding="utf-8")
        return load_config(config_file)

    return factory
