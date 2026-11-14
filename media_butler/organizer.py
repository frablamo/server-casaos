"""Moving a batch into the media library.

Moves are crash-safe: across filesystems the data is copied to a hidden
temporary sibling and renamed into place before the source is deleted.  A
crash therefore leaves either the old state, the new state, or a recognisable
``.mb-tmp-*`` directory that startup cleanup removes.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import shutil
from pathlib import Path

from .util import safe_component

log = logging.getLogger(__name__)

TEMP_PREFIX = ".mb-tmp-"


def destination_path(root: Path, name: str) -> Path:
    """Return a non-existing path inside ``root`` for ``name``.

    Existing names get a `` (1)``, `` (2)``… suffix; nothing is overwritten.
    """
    clean = safe_component(name, limit=160)
    candidate = root / clean
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 10_000):
        candidate = root / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"no free name for {clean} in {root}")


def _copy(src: Path, tmp: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, tmp, symlinks=True)
    else:
        shutil.copy2(src, tmp)


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def move_path(src: Path, dst: Path, item_id: str) -> Path:
    """Move ``src`` to ``dst`` (which must not exist)."""
    if not src.exists():
        raise FileNotFoundError(f"source no longer exists: {src}")
    if dst.exists():
        raise FileExistsError(f"destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.rename(src, dst)
        return dst
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    tmp = dst.parent / f"{TEMP_PREFIX}{item_id}-{secrets.token_hex(3)}"
    if tmp.exists():
        _delete(tmp)
    try:
        _copy(src, tmp)
        os.rename(tmp, dst)
    except BaseException:
        _delete(tmp)
        raise

    try:
        _delete(src)
    except OSError as exc:
        # The new copy is complete; the leftover source is reconciled at boot.
        log.warning("moved to %s but could not delete the source %s: %s", dst, src, exc)
    return dst


def remove_source(path: Path) -> None:
    if path.exists():
        _delete(path)


def move_to_destination(
    source: Path,
    destination_root: Path,
    display_name: str,
    item_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    dst = destination_path(destination_root, display_name)
    if dry_run:
        log.info("[dry-run] would have moved %s → %s", source, dst)
        return dst
    return move_path(source, dst, item_id)


def cleanup_temp_dirs(*roots: Path) -> int:
    removed = 0
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name.startswith(TEMP_PREFIX):
                log.warning("removing leftover temporary move %s", child)
                _delete(child)
                removed += 1
    return removed


def usage(root: Path) -> tuple[int, int, int]:
    stats = shutil.disk_usage(root)
    return stats.total, stats.used, stats.free
