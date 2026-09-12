"""Counterfeit-drive detection tests (no real hardware).

Exercises ``core.fake_detect`` detection logic only: kernel32-level
primitives (open/seek/read/write/close) are faked so no drive is ever
touched, but the pattern matching, short-read, open/seek failure and
write-back compare paths all run for real.
"""

import hashlib
import struct

from core import fake_detect as fd

PROBE = fd.PROBE_SIZE
WRITE = fd.WRITE_BACK_SIZE


class _FakeK32:
    def CloseHandle(self, handle) -> int:
        return 1


def _fake_kernel32(monkeypatch):
    monkeypatch.setattr(fd, "kernel32", lambda: _FakeK32())


def _mixed(n):
    return (bytes(range(256)) * (n // 256 + 1))[:n]


def _expected_pattern(offset: int) -> bytes:
    seed = hashlib.sha256(struct.pack("<Q", offset)).digest()
    return (seed * (WRITE // len(seed) + 1))[:WRITE]


# --- non-destructive probe -------------------------------------------------


def test_probe_passes_for_mixed_data(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: _mixed(n))

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is False
    assert "passed" in msg


def test_probe_reads_from_end_of_reported_capacity(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    seen: list[int] = []
    monkeypatch.setattr(
        fd, "_seek", lambda h, off: seen.append(off) or None
    )
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: _mixed(n))

    reported = 64 * 2**30
    fd.probe_capacity(r"\\.\PHYSICALDRIVE1", reported)

    assert seen == [reported - PROBE]


def test_probe_uses_zero_offset_when_reported_small(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    seen: list[int] = []
    monkeypatch.setattr(
        fd, "_seek", lambda h, off: seen.append(off) or None
    )
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: _mixed(n))

    fd.probe_capacity(r"\\.\PHYSICALDRIVE1", PROBE // 4)

    assert seen == [0]


def test_probe_flags_uniform_data(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: b"\xAA" * n)

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "identical bytes" in msg


def test_probe_flags_short_read(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: _mixed(n // 2))

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "short read" in msg


def test_probe_flags_open_failure(monkeypatch):
    def _fail(path):
        raise OSError("access denied")

    monkeypatch.setattr(fd, "_open_for_read", _fail)

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "cannot open" in msg


def test_probe_flags_seek_failure(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())

    def _fail(h, off):
        raise OSError("seek beyond end")

    monkeypatch.setattr(fd, "_seek", _fail)

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "read probe failed" in msg


def test_probe_flags_read_failure(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)

    def _fail(h, n):
        raise OSError("media error")

    monkeypatch.setattr(fd, "_read_chunk", _fail)

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "read probe failed" in msg


# --- destructive write-back ------------------------------------------------


def _readable_write_back(monkeypatch, read_data: bytes):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_write", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(
        fd, "_write_chunk", lambda h, data: len(data)
    )
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: read_data)


def test_write_back_passes_when_read_matches(monkeypatch):
    reported = 16 * 2**30
    offset = reported - WRITE
    _readable_write_back(monkeypatch, _expected_pattern(offset))

    suspicious, msg = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", reported)

    assert suspicious is False
    assert "passed" in msg


def test_write_back_confirms_counterfeit_on_mismatch(monkeypatch):
    reported = 16 * 2**30
    _readable_write_back(monkeypatch, b"\x00" * WRITE)

    suspicious, msg = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", reported)

    assert suspicious is True
    assert "CONFIRMED COUNTERFEIT" in msg


def test_write_back_derives_pattern_from_offset(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_write", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    written: list[bytes] = []
    monkeypatch.setattr(
        fd, "_write_chunk", lambda h, data: written.append(data) or len(data)
    )
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: written[0][:n])

    reported = 48 * 2**30
    suspicious, _ = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", reported)

    assert suspicious is False
    assert written == [_expected_pattern(reported - WRITE)]


def test_write_back_flags_short_write(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_write", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(fd, "_write_chunk", lambda h, data: len(data) - 1)

    suspicious, msg = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "short write" in msg


def test_write_back_flags_write_open_failure(monkeypatch):
    def _fail(path):
        raise OSError("share violation")

    monkeypatch.setattr(fd, "_open_for_write", _fail)

    suspicious, msg = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "cannot open drive for write-back" in msg


def test_write_back_flags_write_chunk_failure(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_write", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)

    def _fail(h, data):
        raise OSError("write protected")

    monkeypatch.setattr(fd, "_write_chunk", _fail)

    suspicious, msg = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "write-back failed" in msg


def test_write_back_flags_readback_open_failure(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_write", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(
        fd, "_write_chunk", lambda h, data: len(data)
    )

    def _fail(path):
        raise OSError("volume gone")

    monkeypatch.setattr(fd, "_open_for_read", _fail)

    suspicious, msg = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "cannot open drive for read-back" in msg


def test_write_back_flags_readback_failure(monkeypatch):
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_write", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(
        fd, "_write_chunk", lambda h, data: len(data)
    )
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())

    def _fail(h, n):
        raise OSError("io device error")

    monkeypatch.setattr(fd, "_read_chunk", _fail)

    suspicious, msg = fd.write_back_verify(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is True
    assert "read-back failed" in msg


# --- uniform-data helper ---------------------------------------------------


def test_is_uniform_matches_one_byte():
    assert fd._is_uniform(b"\xAA" * 4096) is True
    assert fd._is_uniform(b"\x00" * 4096) is True
    assert fd._is_uniform(_mixed(4096)) is False


def test_is_uniform_edge_cases():
    assert fd._is_uniform(b"") is True
    assert fd._is_uniform(b"\x01") is True