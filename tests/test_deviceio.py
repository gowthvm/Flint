"""Short-write retry behaviour of core.deviceio.write_bytes_retry.

The old implementation resent the whole buffer after a short write, which
duplicated the bytes that had already landed because the file pointer had
advanced.  These tests model that cursor: every accepted byte moves it.
"""

import ctypes

from core import deviceio


class _FakeKernel:
    """WriteFile that models an advancing file-pointer cursor."""

    def __init__(self, capacity: int = 1 << 20, transient_failures: int = 0):
        self.target = bytearray()
        self.cursor = 0
        self.capacity = capacity  # most bytes accepted per call
        self.calls: list[int] = []  # requested size per call
        self.error = 0
        self.transient_left = transient_failures

    def WriteFile(self, handle, buf, size, written_ptr, overlapped):
        self.calls.append(size)
        if self.transient_left > 0:
            self.transient_left -= 1
            self.error = 1117  # ERROR_IO_DEVICE (transient)
            return 0
        self.error = 0
        data = ctypes.string_at(buf, size)
        accepted = min(size, self.capacity)
        if len(self.target) < self.cursor + accepted:
            self.target.extend(b"\x00" * (self.cursor + accepted - len(self.target)))
        self.target[self.cursor : self.cursor + accepted] = data[:accepted]
        self.cursor += accepted
        written_ptr._obj.value = accepted
        return 1

    def GetLastError(self):
        return self.error


def _write(monkeypatch, fake, payload: bytes, **kwargs) -> None:
    monkeypatch.setattr(deviceio, "kernel32", lambda: fake)
    monkeypatch.setattr(deviceio.time, "sleep", lambda _s: None)
    deviceio.write_bytes_retry(ctypes.c_void_p(1), payload, **kwargs)


def test_short_write_resumes_at_unwritten_byte(monkeypatch):
    fake = _FakeKernel(capacity=4)  # every call lands at most 4 bytes
    payload = bytes(range(16))

    _write(monkeypatch, fake, payload, max_retries=10)

    assert bytes(fake.target) == payload, "prefix duplicated or tail lost"
    assert fake.cursor == len(payload)
    assert fake.calls == [16, 12, 8, 4], "each retry must resume at the offset"


def test_transient_error_retries_full_buffer_without_loss(monkeypatch):
    fake = _FakeKernel(transient_failures=2)
    payload = b"flint-payload"

    _write(monkeypatch, fake, payload, max_retries=5)

    assert bytes(fake.target) == payload
    assert fake.calls == [13, 13, 13]


def test_short_write_gives_up_with_oserror(monkeypatch):
    fake = _FakeKernel(capacity=0)  # device accepts nothing

    try:
        _write(monkeypatch, fake, b"0123456789", max_retries=2)
    except OSError as exc:
        assert "short write" in str(exc)
    else:
        raise AssertionError("expected OSError after exhausting retries")
