from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from media_butler import archive


SAMPLE_LISTING = """
7-Zip 23.01 (x64) : Copyright (c) 1999-2023 Igor Pavlov : 2023-06-20

Scanning the drive for archives:
1 file, 100 bytes (1 KiB)

Listing archive: Movie.zip

--
Path = Movie.zip
Type = zip
Physical Size = 100

----------
Path = Movie.mkv
Size = 90
Attributes = A
Encrypted = -

Path = Subs/
Size = 0
Attributes = D
Encrypted = -

Path = Subs/eng.srt
Size = 10
Attributes = A
Encrypted = -
"""


def test_parse_listing_skips_archive_header():
    entries = archive.parse_listing(SAMPLE_LISTING)
    assert [entry.path for entry in entries] == ["Movie.mkv", "Subs/", "Subs/eng.srt"]


def test_validate_entries_rejects_traversal():
    entries = [archive.ArchiveEntry(path="../evil.sh", attributes="A", symbolic_link="", encrypted=False)]
    with pytest.raises(archive.UnsafeArchive):
        archive.validate_entries(entries)


def test_validate_entries_rejects_absolute():
    entries = [archive.ArchiveEntry(path="/etc/passwd", attributes="A", symbolic_link="", encrypted=False)]
    with pytest.raises(archive.UnsafeArchive):
        archive.validate_entries(entries)


def test_validate_entries_rejects_symlink():
    entries = [
        archive.ArchiveEntry(path="link", attributes="A", symbolic_link="/etc/passwd", encrypted=False)
    ]
    with pytest.raises(archive.UnsafeArchive):
        archive.validate_entries(entries)


def test_validate_entries_rejects_encrypted():
    entries = [archive.ArchiveEntry(path="a.mkv", attributes="A", symbolic_link="", encrypted=True)]
    with pytest.raises(archive.PasswordProtected):
        archive.validate_entries(entries)


def test_validate_entries_accepts_normal():
    entries = archive.parse_listing(SAMPLE_LISTING)
    archive.validate_entries(entries)


def test_extraction_name_is_stable():
    first = archive.extraction_name("Movie.2021", "100:1234")
    second = archive.extraction_name("Movie.2021", "100:1234")
    assert first == second
    assert first.startswith("Movie.2021-")


def test_cleanup_incomplete(tmp_path: Path):
    extract_dir = tmp_path / "_extracted"
    extract_dir.mkdir()
    (extract_dir / "half").mkdir()
    done = extract_dir / "full"
    done.mkdir()
    (extract_dir / "full.done").write_text("ok")
    (extract_dir / "orphan.done").write_text("ok")

    archive.cleanup_incomplete(extract_dir)

    assert not (extract_dir / "half").exists()
    assert done.exists()
    assert not (extract_dir / "orphan.done").exists()


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
    print("Path = Movie.mkv")
    print("Size = 4")
    print("Attributes = A")
    print("Encrypted = -")
    sys.exit(0)

if args and args[0] == "x":
    target = next((arg[2:] for arg in args if arg.startswith("-o")), None)
    assert target
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "Movie.mkv"), "wb") as handle:
        handle.write(b"data")
    sys.exit(0)

sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_ensure_extracted_and_reuse(tmp_path: Path):
    seven_zip = _fake_seven_zip(tmp_path)
    source = tmp_path / "Movie.2021.zip"
    source.write_text("archive")
    extract_dir = tmp_path / "_extracted"

    target, fingerprint = archive.ensure_extracted(source, extract_dir, str(seven_zip))
    assert (target / "Movie.mkv").read_text() == "data"
    assert (extract_dir / f"{target.name}.done").is_file()
    assert fingerprint.startswith(str(source.stat().st_size))

    again, _ = archive.ensure_extracted(source, extract_dir, str(seven_zip))
    assert again == target


def test_ensure_extracted_reports_password(tmp_path: Path):
    script = tmp_path / "fake7z"
    script.write_text(
        """#!/usr/bin/env python3
import sys
print("Enter password (Error = Wrong password)")
sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    source = tmp_path / "Secret.zip"
    source.write_text("archive")

    with pytest.raises(archive.PasswordProtected):
        archive.ensure_extracted(source, tmp_path / "_extracted", str(script))
