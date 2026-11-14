from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from media_butler import archive


def test_archive_entry_sizes_are_parsed():
    entries = archive.parse_listing(
        """
--
Path = x.zip
Type = zip
Physical Size = 1

----------
Path = a.mkv
Size = 123
Attributes = A

Path = empty/
Size = 0
Attributes = D
"""
    )
    assert [(entry.path, entry.size) for entry in entries] == [("a.mkv", 123), ("empty/", 0)]


def test_insufficient_space_is_reported(tmp_path: Path):
    script = tmp_path / "fake7z"
    script.write_text(
        """#!/usr/bin/env python3
import sys
print("--")
print("Path = x.zip")
print("Type = zip")
print("Physical Size = 1")
print()
print("----------")
print()
print("Path = huge.mkv")
print("Size = 1000000000000000")
print("Attributes = A")
sys.exit(0)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    source = tmp_path / "Huge.zip"
    source.write_text("archive")

    with pytest.raises(archive.InsufficientSpace):
        archive.ensure_extracted(source, tmp_path / "_extracted", str(script))

    assert not (tmp_path / "_extracted").exists()


def test_space_check_uses_available_size(tmp_path: Path, monkeypatch):
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(usage.total, usage.used, 1024),
    )
    entries = [archive.ArchiveEntry(path="a", attributes="A", symbolic_link="", encrypted=False, size=4096)]
    with pytest.raises(archive.InsufficientSpace):
        archive._check_free_space(tmp_path / "x.zip", entries)
