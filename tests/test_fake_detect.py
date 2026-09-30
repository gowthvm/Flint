"""Counterfeit-drive detection tests (no real hardware).

Exercises ``core.fake_detect`` detection logic only: kernel32-level
primitives (open/seek/read/close) are faked so no drive is ever touched,
but the pattern matching, short-read and open/seek failure paths all run
for real.
"""

from core import fake_detect as fd

PROBE = fd.PROBE_SIZE


class _FakeK32:
    def CloseHandle(self, handle) -> int:
        return 1


def _fake_kernel32(monkeypatch):
    monkeypatch.setattr(fd, "kernel32", lambda: _FakeK32())


def _mixed(n):
    return (bytes(range(256)) * (n // 256 + 1))[:n]


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


def test_probe_reports_uniform_data_as_unverified(monkeypatch):
    """L14: a uniform tail is normal on blank/erased media, so it must be
    reported as *unverified* (suspicious=False), not as a counterfeit.

    The CLI only blocks when suspicious is True, so flipping this back
    would abort every flash to a freshly formatted stick without --yes.
    """
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: b"\xAA" * n)

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is False
    assert "unverified" in msg
    assert "identical bytes" in msg


def test_probe_reports_blank_region_as_unverified(monkeypatch):
    """All-zero reads (common for unformatted media) are also not fake."""
    _fake_kernel32(monkeypatch)
    monkeypatch.setattr(fd, "_open_for_read", lambda path: object())
    monkeypatch.setattr(fd, "_seek", lambda h, off: None)
    monkeypatch.setattr(fd, "_read_chunk", lambda h, n: b"\x00" * n)

    suspicious, msg = fd.probe_capacity(r"\\.\PHYSICALDRIVE1", 16 * 2**30)

    assert suspicious is False
    assert msg.startswith("unverified:")


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


# --- uniform-data helper ---------------------------------------------------


def test_is_uniform_matches_one_byte():
    assert fd._is_uniform(b"\xAA" * 4096) is True
    assert fd._is_uniform(b"\x00" * 4096) is True
    assert fd._is_uniform(_mixed(4096)) is False


def test_is_uniform_edge_cases():
    assert fd._is_uniform(b"") is True
    assert fd._is_uniform(b"\x01") is True
