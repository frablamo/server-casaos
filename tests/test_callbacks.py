from __future__ import annotations

import pytest

from media_butler import callbacks


def test_move_roundtrip():
    data = callbacks.encode_move("a1b2c3d4", "movies")
    assert data == "mv:a1b2c3d4:movies"
    assert callbacks.parse_move(data) == ("a1b2c3d4", "movies")


def test_move_rejects_unknown_destination():
    with pytest.raises(ValueError):
        callbacks.encode_move("a1b2c3d4", "../../etc")


def test_move_rejects_bad_id():
    with pytest.raises(ValueError):
        callbacks.encode_move("nope", "kids")


def test_ignore_roundtrip():
    assert callbacks.parse_ignore(callbacks.encode_ignore("deadbeef")) == "deadbeef"


def test_retry_roundtrip():
    assert callbacks.parse_retry(callbacks.encode_retry("deadbeef")) == "deadbeef"


def test_reboot_roundtrip():
    assert callbacks.parse_reboot(callbacks.encode_reboot("deadbeef", "yes")) == ("deadbeef", "yes")
    assert callbacks.parse_reboot("rt:deadbeef:maybe") is None


def test_parser_rejects_garbage():
    assert callbacks.parse_move("mv:x:movies") is None
    assert callbacks.parse_move("mv:a1b2c3d4:music") is None
    assert callbacks.parse_ignore("ig:../../etc") is None
    assert callbacks.parse_reboot("") is None
