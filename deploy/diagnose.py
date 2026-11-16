"""Explain what the scanner sees in the inbox and why nothing is offered.

Run it with the installed virtualenv, from anywhere:

    sudo /opt/media-butler/venv/bin/python deploy/diagnose.py

It only reads the inbox and opens the state database read-only, so it is safe
to run while the service is up.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from media_butler.config import load_config
from media_butler.db import Database
from media_butler.scanner import Scanner

SKIPPED = ("ignored", "blocked", "done")


def main(config_path: str) -> int:
    config = load_config(config_path)
    inbox = config.paths.inbox

    print("config         :", config_path)
    print("inbox          :", inbox)
    print("allowed_users  :", config.telegram.allowed_users or "EMPTY (the bot never scans)")
    print("chat_id        :", config.telegram.chat_id)
    print("max_pending    :", config.scan.max_pending)
    print("settle/interval:", config.scan.settle_seconds, "/", config.scan.interval_seconds)

    connection: sqlite3.Connection | None = None
    awaiting = 0
    try:
        connection = sqlite3.connect(f"file:{config.database}?mode=ro", uri=True)
        awaiting = connection.execute(
            "select count(*) from items where status in ('pending', 'blocked')"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        print("state database : unreadable:", exc)
    full = awaiting >= config.scan.max_pending
    print(
        "awaiting now   :", awaiting,
        "(QUEUE FULL: scans are skipped)" if full else "",
    )
    print()

    scanner = Scanner(config, None)  # type: ignore[arg-type]
    for entry in sorted(inbox.iterdir()):
        if entry.is_symlink():
            print(f"{entry.name!r}\n   SYMLINK: the scanner ignores it")
            continue
        if not entry.is_dir():
            kind = (
                "video" if config.scan.is_video(entry.name)
                else "archive" if config.scan.is_archive(entry.name)
                else "other"
            )
            print(
                f"{entry.name!r}\n   FILE {kind}, "
                f"partial={config.scan.is_partial(entry.name)}"
            )
            continue

        signature = scanner._tree_signature(entry)
        token = signature.token()
        print(f"{entry.name!r}")
        print(
            f"   videos={signature.video_count} partial={signature.partial} "
            f"files={signature.count} bytes={signature.size_bytes}"
        )
        if signature.video_count == 0:
            print("   no videos inside: the folder is never offered")
        if signature.partial:
            print("   a .part/.!qB file is present: it is never considered stable")
        rows = (
            connection.execute(
                "select status, fingerprint from items where source = ?",
                (str(entry),),
            ).fetchall()
            if connection is not None
            else []
        )
        if not rows:
            print("   DB: no rows for this path")
        for status, fingerprint in rows:
            note = ""
            if status in SKIPPED and fingerprint == token:
                note = "  <-- same content, the scanner skips it"
            print(f"   DB: status={status} fingerprint={fingerprint}{note}")
    if connection is not None:
        connection.close()

    print()
    print("What a forced scan would offer (throwaway database):")
    with tempfile.TemporaryDirectory() as temporary:
        database = Database(Path(temporary) / "state.db")
        test = Scanner(config, database)
        test.scan_once()
        batches = test.scan_once(force={str(entry) for entry in inbox.iterdir()})
        print("   batches:", len(batches))
        for batch in batches:
            print(f"   - {batch.kind:8} {batch.display_name!r} ({batch.video_count} videos)")
        database.close()

    print()
    if full:
        print("Verdict: the queue is full; answer or ignore the pending prompts.")
    elif not config.telegram.allowed_users:
        print("Verdict: allowed_users is empty, so the bot never scans.")
    else:
        print(
            "Verdict: if batches > 0 and the bot still reports nothing, check "
            "`journalctl -u media-butler` for 'could not send the prompt' "
            "(wrong chat_id) and restart after editing the config."
        )
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/etc/media-butler/config.toml"
    raise SystemExit(main(path))
