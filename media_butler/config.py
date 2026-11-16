"""Configuration loading and validation.

The bot is configured with a single TOML file (``/etc/media-butler/config.toml``
by default).  Secrets (Telegram token, Emby API key) are read from separate
root-only files so the main config can be inspected without leaking them.
"""

from __future__ import annotations

import os
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CONFIG_PATH = "/etc/media-butler/config.toml"

DEFAULT_VIDEO_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".m2ts",
    ".ts",
    ".vob",
    ".divx",
    ".ogv",
    ".3gp",
)

DEFAULT_ARCHIVE_EXTENSIONS = (
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".gz",
    ".bz2",
    ".xz",
)

DEFAULT_SKIP_SUFFIXES = (
    ".part",
    ".!qb",
    ".crdownload",
    ".aria2",
    ".tmp",
    ".downloading",
)


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _get_str(table: dict[str, Any], key: str, default: str = "") -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string")
    return value.strip()


def _get_int(table: dict[str, Any], key: str, default: int, minimum: int = 0) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{key}' must be an integer")
    if value < minimum:
        raise ConfigError(f"'{key}' must be >= {minimum}")
    return value


def _get_bool(table: dict[str, Any], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be true or false")
    return value


def _get_str_list(table: dict[str, Any], key: str, default: Iterable[str]) -> tuple[str, ...]:
    value = table.get(key)
    if value is None:
        return tuple(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"'{key}' must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


def _get_int_list(table: dict[str, Any], key: str) -> tuple[int, ...]:
    value = table.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"'{key}' must be a list of numeric IDs")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ConfigError(f"'{key}' contains an invalid ID: {item!r}")
        if item:
            result.append(item)
    return tuple(result)


def _reject_non_printable(value: str, key: str) -> str:
    """Refuse paths systemd cannot represent in a unit file.

    A carriage return from a Windows clipboard, a zero-width space or a
    non-breaking space pasted from a web page all look identical on screen but
    make ``ReadWritePaths`` unparseable.  Rejecting them here turns a cryptic
    systemd failure into an actionable message.
    """
    for char in value:
        category = unicodedata.category(char)
        if category in ("Cc", "Cf") or (category == "Zs" and char != " "):
            raise ConfigError(
                f"'{key}' contains a non-printable character U+{ord(char):04X}; "
                "retype the path instead of pasting it"
            )
    return value


def _absolute_path(value: str, key: str) -> Path:
    expanded = os.path.expanduser(_reject_non_printable(value, key))
    path = Path(expanded)
    if not expanded:
        raise ConfigError(f"'{key}' cannot be empty")
    if not path.is_absolute():
        raise ConfigError(f"'{key}' must be an absolute path: {value}")
    return path


def _read_secret(path: Path, key: str) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"secret file for '{key}' does not exist: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read '{key}' from {path}: {exc}") from exc
    if not content:
        raise ConfigError(f"secret file for '{key}' is empty: {path}")
    return content


def _normalise_extensions(values: Iterable[str]) -> tuple[str, ...]:
    normalised: list[str] = []
    for value in values:
        item = value.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        normalised.append(item)
    # Longest first so ".tar.gz" wins over ".gz".
    return tuple(sorted(dict.fromkeys(normalised), key=len, reverse=True))


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    allowed_users: tuple[int, ...]
    chat_id: int

    @property
    def can_send(self) -> bool:
        return self.chat_id != 0


@dataclass(frozen=True)
class PathsConfig:
    inbox: Path
    kids: Path
    movies: Path
    series: Path

    @property
    def destinations(self) -> dict[str, Path]:
        return {"kids": self.kids, "movies": self.movies, "series": self.series}


@dataclass(frozen=True)
class ScanConfig:
    interval_seconds: int
    settle_seconds: int
    max_pending: int
    max_entries: int
    seven_zip: str
    video_extensions: tuple[str, ...]
    archive_extensions: tuple[str, ...]
    skip_suffixes: tuple[str, ...]

    def is_video(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered.endswith(ext) for ext in self.video_extensions)

    def is_archive(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered.endswith(ext) for ext in self.archive_extensions)

    def is_partial(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered.endswith(ext) for ext in self.skip_suffixes)

    def archive_stem(self, name: str) -> str:
        lowered = name.lower()
        for ext in self.archive_extensions:
            if lowered.endswith(ext):
                return name[: -len(ext)]
        return name


@dataclass(frozen=True)
class BehaviorConfig:
    delete_archive: bool
    dry_run: bool


@dataclass(frozen=True)
class EmbyConfig:
    enabled: bool
    url: str
    api_key: str


@dataclass(frozen=True)
class RebootConfig:
    countdown_seconds: int
    cooldown_seconds: int
    notify_on_boot: bool


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    paths: PathsConfig
    scan: ScanConfig
    behavior: BehaviorConfig
    emby: EmbyConfig
    reboot: RebootConfig
    database: Path
    log_level: str
    source: Path

    @property
    def extract_dir(self) -> Path:
        return self.paths.inbox / "_extracted"


def load_config(path: str | os.PathLike[str] | None = None, *, check_paths: bool = True) -> Config:
    config_path = Path(path or os.environ.get("MEDIA_BUTLER_CONFIG", DEFAULT_CONFIG_PATH))
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file does not exist: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {config_path}: {exc}") from exc

    try:
        document = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    telegram_table = _table(document, "telegram")
    paths_table = _table(document, "paths")
    scan_table = _table(document, "scan")
    behavior_table = _table(document, "behavior")
    emby_table = _table(document, "emby")
    reboot_table = _table(document, "reboot")
    logging_table = _table(document, "logging")

    token = _get_str(telegram_table, "token")
    token_file = _get_str(telegram_table, "token_file")
    if not token:
        if not token_file:
            raise ConfigError("missing 'telegram.token' or 'telegram.token_file'")
        token = _read_secret(_absolute_path(token_file, "telegram.token_file"), "telegram.token")
    if not token:
        raise ConfigError("the Telegram token is empty")

    allowed_users = _get_int_list(telegram_table, "allowed_users")
    chat_id = _get_int(telegram_table, "chat_id", 0, minimum=-(2**63))
    if chat_id == 0 and allowed_users:
        chat_id = allowed_users[0]

    inbox = _absolute_path(_get_str(paths_table, "inbox"), "paths.inbox")
    kids = _absolute_path(_get_str(paths_table, "kids"), "paths.kids")
    movies = _absolute_path(_get_str(paths_table, "movies"), "paths.movies")
    series = _absolute_path(_get_str(paths_table, "series"), "paths.series")

    destinations = {"kids": kids, "movies": movies, "series": series}
    for name, destination in destinations.items():
        if destination == inbox:
            raise ConfigError(f"paths.{name} cannot be the same as paths.inbox")
        if destination in inbox.parents or inbox in destination.parents:
            raise ConfigError(f"paths.{name} and paths.inbox cannot be nested inside each other")

    scan = ScanConfig(
        interval_seconds=_get_int(scan_table, "interval_seconds", 3600, minimum=30),
        settle_seconds=_get_int(scan_table, "settle_seconds", 120, minimum=0),
        max_pending=_get_int(scan_table, "max_pending", 10, minimum=1),
        max_entries=_get_int(scan_table, "max_entries", 100_000, minimum=1),
        seven_zip=_get_str(scan_table, "seven_zip", "7z") or "7z",
        video_extensions=_normalise_extensions(
            _get_str_list(scan_table, "video_extensions", DEFAULT_VIDEO_EXTENSIONS)
        ),
        archive_extensions=_normalise_extensions(
            _get_str_list(scan_table, "archive_extensions", DEFAULT_ARCHIVE_EXTENSIONS)
        ),
        skip_suffixes=_normalise_extensions(
            _get_str_list(scan_table, "skip_suffixes", DEFAULT_SKIP_SUFFIXES)
        ),
    )

    behavior = BehaviorConfig(
        delete_archive=_get_bool(behavior_table, "delete_archive", True),
        dry_run=_get_bool(behavior_table, "dry_run", False),
    )

    emby_url = _get_str(emby_table, "url")
    emby_enabled = _get_bool(emby_table, "enabled", bool(emby_url))
    emby_api_key = _get_str(emby_table, "api_key")
    emby_key_file = _get_str(emby_table, "api_key_file")
    if emby_enabled:
        if not emby_url:
            raise ConfigError("emby.enabled is on but 'emby.url' is missing")
        if not emby_api_key and emby_key_file:
            emby_api_key = _read_secret(
                _absolute_path(emby_key_file, "emby.api_key_file"), "emby.api_key"
            )
        if not emby_api_key:
            raise ConfigError("emby.enabled is on but 'emby.api_key' or 'emby.api_key_file' is missing")

    reboot = RebootConfig(
        countdown_seconds=_get_int(reboot_table, "countdown_seconds", 10, minimum=0),
        cooldown_seconds=_get_int(reboot_table, "cooldown_seconds", 300, minimum=0),
        notify_on_boot=_get_bool(reboot_table, "notify_on_boot", True),
    )

    database = _absolute_path(
        _get_str(document, "database", "/var/lib/media-butler/state.db"),
        "database",
    )
    log_level = (_get_str(logging_table, "level", "INFO") or "INFO").upper()

    config = Config(
        telegram=TelegramConfig(
            token=token,
            allowed_users=allowed_users,
            chat_id=chat_id,
        ),
        paths=PathsConfig(inbox=inbox, kids=kids, movies=movies, series=series),
        scan=scan,
        behavior=behavior,
        emby=EmbyConfig(enabled=emby_enabled, url=emby_url.rstrip("/"), api_key=emby_api_key),
        reboot=reboot,
        database=database,
        log_level=log_level,
        source=config_path,
    )

    if check_paths:
        _check_paths(config)

    return config


def _check_paths(config: Config) -> None:
    if not config.paths.inbox.is_dir():
        raise ConfigError(f"inbox does not exist or is not a directory: {config.paths.inbox}")
    for name, destination in config.paths.destinations.items():
        if not destination.is_dir():
            raise ConfigError(
                f"destination '{name}' does not exist or is not a directory: {destination} "
                "(create it before starting the bot)"
            )
    try:
        config.database.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"could not create the state directory {config.database.parent}: {exc}") from exc
