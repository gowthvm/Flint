"""Tests for core.eject — GUID encoding helpers."""

import struct

from core.eject import _encode_guid, _guid_bytes


def test_encode_guid_known_value():
    guid = (0x12345678, 0xABCD, 0xEF01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF, 0x01)
    result = _encode_guid(guid)
    assert isinstance(result, bytes)
    assert len(result) == 16


def test_encode_guid_roundtrip():
    original = (0x01020304, 0x0506, 0x0708, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10)
    encoded = _encode_guid(original)
    decoded = struct.unpack_from("<IHH8B", encoded)
    assert decoded[0] == original[0]
    assert decoded[1] == original[1]
    assert decoded[2] == original[2]


def test_encode_guid_all_zeros():
    result = _encode_guid((0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    assert result == b"\x00" * 16


def test_encode_guid_all_ones():
    result = _encode_guid((0xFFFFFFFF, 0xFFFF, 0xFFFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF))
    assert result == b"\xff" * 16


def test_guid_bytes_returns_array():
    result = _guid_bytes((1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11))
    assert len(result) == 16
