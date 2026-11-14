"""Command line entry point."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .bot import MediaButlerBot
from .config import Config, ConfigError, load_config
from .db import Database
from .util import human_size


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-butler",
        description="Telegram bot that sorts downloads and can reboot the server.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("MEDIA_BUTLER_CONFIG", "/etc/media-butler/config.toml"),
        help="path to config.toml (default: %(default)s)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration, paths, write permissions and 7z; do not start the bot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do not move, delete or reboot anything; useful to test the buttons",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    return parser


def _check_runtime(config: Config) -> int:
    ok = True
    print(f"media-butler {__version__}")
    print(f"config: {config.source}")
    print(f"state:  {config.database}")
    print(f"inbox:  {config.paths.inbox}")

    seven_zip = shutil.which(config.scan.seven_zip)
    if seven_zip:
        print(f"7z:     {seven_zip}")
    else:
        print(f"7z:     ✗ not found ({config.scan.seven_zip})", file=sys.stderr)
        ok = False

    for label, path in {"kids": config.paths.kids, "movies": config.paths.movies, "series": config.paths.series}.items():
        try:
            free = shutil.disk_usage(path).free
            print(f"{label:7s} {path} ({human_size(free)} free)")
        except OSError as exc:
            print(f"{label:7s} ✗ {path}: {exc}", file=sys.stderr)
            ok = False

    targets = {"inbox": config.paths.inbox, **config.paths.destinations}
    for label, path in targets.items():
        probe = Path(path) / f".mb-write-test-{os.getpid()}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
            print(f"write {label}: ✓")
        except OSError as exc:
            print(f"write {label}: ✗ {path}: {exc}", file=sys.stderr)
            ok = False

    try:
        config.database.parent.mkdir(parents=True, exist_ok=True)
        probe = config.database.parent / f".mb-write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        print("write state: ✓")
    except OSError as exc:
        print(f"write state: ✗ {config.database.parent}: {exc}", file=sys.stderr)
        ok = False

    if config.emby.enabled:
        print(f"emby:   {config.emby.url}")
    else:
        print("emby:   disabled")

    if not config.telegram.allowed_users:
        print(
            "warning: allowed_users is empty; send /start to the bot and copy the user_id",
            file=sys.stderr,
        )

    print("result: OK" if ok else "result: ERRORS FOUND", file=sys.stderr)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        config = dataclasses.replace(
            config, behavior=dataclasses.replace(config.behavior, dry_run=True)
        )

    logging.basicConfig(
        level=getattr(logging, (args.log_level or config.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if args.check:
        return _check_runtime(config)

    db = Database(config.database)
    try:
        MediaButlerBot(config, db).run()
    except KeyboardInterrupt:
        return 130
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
