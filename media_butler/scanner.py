"""Inbox scanner.

A file or directory is only considered once its signature has been stable for
``settle_seconds``.  That avoids grabbing something a downloader is still
writing.  Archives are inspected and extracted before being offered.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import archive as archive_tools
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

EXTRACTED_DIR_NAME = "_extracted"


def is_secondary_volume(name: str) -> bool:
    """True for ``foo.part2.rar``, ``foo.r01``… but not the first volume."""
    lowered = name.lower()
    match = re.search(r"\.part(\d+)\.rar$", lowered)
    if match:
        return int(match.group(1)) > 1
    return bool(re.search(r"\.r\d{2,3}$", lowered))


@dataclass(frozen=True)
class Signature:
    count: int
    size_bytes: int
    newest_ns: int
    video_count: int
    partial: bool

    def token(self) -> str:
        return f"{self.count}:{self.size_bytes}:{self.newest_ns}:{self.video_count}:{int(self.partial)}"


@dataclass(frozen=True)
class Batch:
    source: Path
    kind: str
    display_name: str
    size_bytes: int
    video_count: int
    fingerprint: str
    archive_path: Path | None = None
    problem: str | None = None


class Scanner:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self._seen: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------- signatures
    def _file_signature(self, path: Path) -> Signature:
        stat = path.stat()
        video = 1 if self.config.scan.is_video(path.name) else 0
        return Signature(1, stat.st_size, stat.st_mtime_ns, video, False)

    def _tree_signature(self, root: Path) -> Signature:
        count = 0
        size = 0
        newest = 0
        videos = 0
        partial = False
        max_entries = self.config.scan.max_entries

        for current_root, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(current_root) / name).is_symlink()]
            for name in files:
                if count >= max_entries:
                    log.warning("too many files in %s, treating the batch as unstable", root)
                    return Signature(count, size, newest, videos, True)
                child = Path(current_root) / name
                if child.is_symlink():
                    continue
                if self.config.scan.is_partial(name):
                    partial = True
                try:
                    stat = child.stat()
                except OSError:
                    continue
                count += 1
                size += stat.st_size
                newest = max(newest, stat.st_mtime_ns)
                if self.config.scan.is_video(name):
                    videos += 1
        return Signature(count, size, newest, videos, partial)

    def _is_stable(
        self, path: Path, signature: Signature, now: float, *, force: bool = False
    ) -> bool:
        if signature.partial:
            self._seen.pop(str(path), None)
            return False
        if signature.count == 0 or signature.size_bytes <= 0:
            return False
        if force:
            # The user explicitly retried this batch; do not make them wait
            # for another full scan (which can be an hour away).
            self._seen.pop(str(path), None)
            return True
        key = str(path)
        token = signature.token()
        previous = self._seen.get(key)
        if previous is None or previous[0] != token:
            self._seen[key] = (token, now)
            return False
        return now - previous[1] >= self.config.scan.settle_seconds

    # ------------------------------------------------------------------ scan
    def scan_once(self, force: set[str] | None = None) -> list[Batch]:
        forced = {str(path) for path in (force or ())}
        inbox = self.config.paths.inbox
        try:
            with os.scandir(inbox) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except FileNotFoundError:
            log.error("inbox does not exist: %s", inbox)
            return []
        except OSError as exc:
            log.error("could not read inbox %s: %s", inbox, exc)
            return []

        now = time.monotonic()
        batches: list[Batch] = []
        seen_now: set[str] = set()

        for entry in entries:
            name = entry.name
            if name.startswith(".") or name == EXTRACTED_DIR_NAME:
                continue
            path = Path(entry.path)
            if path.is_symlink() or not path.exists():
                continue
            if self.config.scan.is_partial(name):
                continue
            seen_now.add(str(path))

            try:
                if path.is_dir():
                    batch = self._folder_batch(path, now, str(path) in forced)
                elif self.config.scan.is_archive(name):
                    batch = self._archive_batch(path, now, str(path) in forced)
                elif self.config.scan.is_video(name):
                    batch = self._video_batch(path, now, str(path) in forced)
                else:
                    log.debug("ignoring non-media file: %s", path)
                    continue
            except OSError as exc:
                log.warning("could not inspect %s: %s", path, exc)
                continue

            if batch is not None:
                batches.append(batch)

        for key in list(self._seen):
            if key not in seen_now:
                del self._seen[key]

        return batches

    # ----------------------------------------------------------------- batches
    def _folder_batch(self, path: Path, now: float, force: bool = False) -> Batch | None:
        signature = self._tree_signature(path)
        if not self._is_stable(path, signature, now, force=force):
            return None
        if signature.video_count == 0:
            log.debug("folder without videos, leaving it in the inbox: %s", path)
            return None
        if self.db.should_skip(str(path), signature.token()):
            return None
        return Batch(
            source=path,
            kind="folder",
            display_name=path.name,
            size_bytes=signature.size_bytes,
            video_count=signature.video_count,
            fingerprint=signature.token(),
        )

    def _video_batch(self, path: Path, now: float, force: bool = False) -> Batch | None:
        signature = self._file_signature(path)
        if not self._is_stable(path, signature, now, force=force):
            return None
        if self.db.should_skip(str(path), signature.token()):
            return None
        return Batch(
            source=path,
            kind="video",
            display_name=path.name,
            size_bytes=signature.size_bytes,
            video_count=1,
            fingerprint=signature.token(),
        )

    def _archive_batch(self, path: Path, now: float, force: bool = False) -> Batch | None:
        if is_secondary_volume(path.name):
            log.debug("secondary volume of a multi-part archive, ignoring: %s", path)
            return None
        target, fingerprint = archive_tools.extraction_target(path, self.config.extract_dir)
        if self.db.archive_known(str(path), fingerprint):
            return None

        signature = self._file_signature(path)
        if not self._is_stable(path, signature, now, force=force):
            return None

        stem = self.config.scan.archive_stem(path.name)
        try:
            target, fingerprint = archive_tools.ensure_extracted(
                path, self.config.extract_dir, self.config.scan.seven_zip
            )
        except archive_tools.ArchiveError as exc:
            log.warning("archive blocked %s: %s", path, exc)
            return Batch(
                source=path,
                kind="blocked",
                display_name=stem,
                size_bytes=signature.size_bytes,
                video_count=0,
                fingerprint=fingerprint,
                archive_path=path,
                problem=str(exc),
            )

        if self.db.should_skip(str(target), fingerprint):
            return None

        extracted = self._tree_signature(target)
        if extracted.video_count == 0:
            log.warning("archive %s contains no videos; leaving it untouched", path)
            return Batch(
                source=path,
                kind="blocked",
                display_name=stem,
                size_bytes=signature.size_bytes,
                video_count=0,
                fingerprint=fingerprint,
                archive_path=path,
                problem="the archive contains no videos",
            )
        return Batch(
            source=target,
            kind="archive",
            display_name=stem,
            size_bytes=extracted.size_bytes,
            video_count=extracted.video_count,
            fingerprint=fingerprint,
            archive_path=path,
        )
