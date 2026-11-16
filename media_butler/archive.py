"""Archive inspection and extraction.

Extraction runs as root, so this module is deliberately paranoid: before a
single byte is written, the archive index is parsed and rejected if it
contains absolute paths, ``..`` traversal or symlinks.  ``7z`` handles zip,
7z, rar and tarballs uniformly.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .util import human_size, safe_component

log = logging.getLogger(__name__)

LIST_TIMEOUT = 120
EXTRACT_TIMEOUT = 6 * 60 * 60
SPACE_MARGIN = 2 * 1024**3
DRIVE_RE = re.compile(r"^[A-Za-z]:")
PART_VOLUME_RE = re.compile(r"^(?P<stem>.+?)\.part\d+\.rar$", re.IGNORECASE)
OLD_VOLUME_RE = re.compile(r"^(?P<stem>.+?)\.(?:rar|r\d{2,3}|s\d{2,3})$", re.IGNORECASE)


class ArchiveError(Exception):
    """Base class for archive problems that need a human."""


class ToolUnavailable(ArchiveError):
    pass


class PasswordProtected(ArchiveError):
    pass


class UnsafeArchive(ArchiveError):
    pass


class CorruptArchive(ArchiveError):
    pass


class InsufficientSpace(ArchiveError):
    pass


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    attributes: str
    symbolic_link: str
    encrypted: bool
    size: int = 0


def _run(command: list[str], *, timeout: int, stdin_null: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL if stdin_null else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolUnavailable(f"executable not found: '{command[0]}'") from exc
    except subprocess.TimeoutExpired as exc:
        raise CorruptArchive(f"'{command[0]}' took too long ({timeout}s)") from exc


def parse_listing(output: str) -> list[ArchiveEntry]:
    """Parse the ``7z l -slt`` output into entries (archive record excluded)."""
    entries: list[ArchiveEntry] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if not current:
            return
        if "Path" not in current:
            return
        if "Type" in current or "Physical Size" in current:
            # Archive header block, not a file inside it.
            return
        entries.append(
            ArchiveEntry(
                path=current["Path"],
                attributes=current.get("Attributes", ""),
                symbolic_link=current.get("Symbolic Link", ""),
                encrypted=current.get("Encrypted", "").strip() == "+",
                size=_to_int(current.get("Size")),
            )
        )

    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            flush()
            current = {}
            continue
        if line.strip() == "----------":
            flush()
            current = {}
            continue
        key, separator, value = line.partition(" = ")
        if not separator:
            continue
        current[key.strip()] = value.strip()
    flush()
    return entries


def _to_int(value: str | None) -> int:
    try:
        return int((value or "0").strip())
    except ValueError:
        return 0


def validate_entries(entries: list[ArchiveEntry]) -> None:
    for entry in entries:
        path = entry.path.replace("\\", "/")
        if entry.encrypted:
            raise PasswordProtected("the archive is encrypted")
        if entry.symbolic_link:
            raise UnsafeArchive(f"contains a symbolic link: {entry.path}")
        attributes = entry.attributes.lower()
        if " l" in f" {attributes}" and "lrwx" in attributes:
            raise UnsafeArchive(f"contains a symbolic link: {entry.path}")
        if path.startswith("/") or path.startswith("//") or DRIVE_RE.match(path):
            raise UnsafeArchive(f"absolute path inside the archive: {entry.path}")
        parts = [part for part in path.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise UnsafeArchive(f"path with '..' inside the archive: {entry.path}")


def list_archive(archive: Path, seven_zip: str) -> list[ArchiveEntry]:
    result = _run(
        [seven_zip, "l", "-slt", "-p", str(archive)],
        timeout=LIST_TIMEOUT,
        stdin_null=True,
    )
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode != 0:
        if "password" in combined or "encrypted" in combined:
            raise PasswordProtected("the archive is password protected")
        raise CorruptArchive(f"could not read the archive index: {_tail(result.stderr) or _tail(result.stdout)}")
    entries = parse_listing(result.stdout)
    validate_entries(entries)
    return entries


def _tail(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text[-limit:]


def _check_free_space(archive: Path, entries: list[ArchiveEntry]) -> None:
    required = sum(entry.size for entry in entries)
    if required <= 0:
        return
    try:
        free = shutil.disk_usage(archive.parent).free
    except OSError:
        return
    if required + SPACE_MARGIN > free:
        raise InsufficientSpace(
            f"needs ~{human_size(required)} but only {human_size(free)} is free"
        )


def fingerprint_file(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def volume_files(archive: Path) -> list[Path]:
    """Return every file that belongs to ``archive``'s multi-volume set.

    A single-file archive returns just itself.  Both common RAR layouts are
    recognised: ``foo.part1.rar``/``foo.part2.rar`` and the old
    ``foo.rar``/``foo.r00``/``foo.s00`` scheme.  The bot uses this to delete
    all volumes once the contents have been moved, not only the first one.
    """
    name = archive.name
    match = PART_VOLUME_RE.match(name)
    if match:
        pattern = re.compile(
            rf"^{re.escape(match.group('stem'))}\.part\d+\.rar$", re.IGNORECASE
        )
    else:
        match = OLD_VOLUME_RE.match(name)
        if not match:
            return [archive]
        pattern = re.compile(
            rf"^{re.escape(match.group('stem'))}\.(?:rar|r\d{{2,3}}|s\d{{2,3}})$",
            re.IGNORECASE,
        )

    try:
        siblings = list(archive.parent.iterdir())
    except OSError:
        return [archive]
    return sorted(
        (path for path in siblings if path.is_file() and pattern.match(path.name)),
        key=lambda path: path.name,
    )


def extraction_name(stem: str, fingerprint: str) -> str:
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
    return f"{safe_component(stem)}-{digest}"


def _stem(name: str) -> str:
    lowered = name.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar"):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def extraction_target(archive: Path, extract_dir: Path) -> tuple[Path, str]:
    """Where ``archive`` would be extracted, plus its fingerprint."""
    fingerprint = fingerprint_file(archive)
    return extract_dir / extraction_name(_stem(archive.name), fingerprint), fingerprint


def ensure_extracted(archive: Path, extract_dir: Path, seven_zip: str) -> tuple[Path, str]:
    """Extract ``archive`` once and return (directory, fingerprint)."""
    target, fingerprint = extraction_target(archive, extract_dir)
    marker = extract_dir / f"{target.name}.done"

    if marker.is_file() and target.is_dir():
        return target, fingerprint

    if target.exists():
        log.warning("leftover incomplete extraction, removing %s", target)
        shutil.rmtree(target, ignore_errors=True)
    marker.unlink(missing_ok=True)

    log.info("inspecting %s", archive)
    entries = list_archive(archive, seven_zip)
    _check_free_space(archive, entries)

    extract_dir.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True)
    log.info("extracting %s → %s", archive, target)
    result = _run(
        [seven_zip, "x", "-y", "-bso0", "-bsp0", "-p", f"-o{target}", str(archive)],
        timeout=EXTRACT_TIMEOUT,
        stdin_null=True,
    )
    if result.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "password" in combined or "encrypted" in combined:
            raise PasswordProtected("the archive is password protected")
        raise CorruptArchive(f"extraction failed: {_tail(result.stderr) or _tail(result.stdout)}")

    marker.write_text("ok\n", encoding="utf-8")
    return target, fingerprint


def cleanup_incomplete(extract_dir: Path) -> None:
    """Remove extraction directories left behind by a crash."""
    if not extract_dir.is_dir():
        return
    for child in extract_dir.iterdir():
        try:
            if child.name.endswith(".done"):
                directory = extract_dir / child.name[: -len(".done")]
                if not directory.is_dir():
                    child.unlink(missing_ok=True)
                continue
            if child.is_dir() and not (extract_dir / f"{child.name}.done").is_file():
                log.warning("removing incomplete extraction %s", child)
                shutil.rmtree(child, ignore_errors=True)
        except OSError as exc:
            log.warning("could not clean up %s: %s", child, exc)


def remove_extraction(target: Path, extract_dir: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    (extract_dir / f"{target.name}.done").unlink(missing_ok=True)
