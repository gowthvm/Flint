"""Tests for core.benchmark — read/write benchmarks."""

import ctypes

from core.benchmark import benchmark_read, benchmark_write, estimate_write_time


def _fake_kernel32(monkeypatch):
    """Replace kernel32 with a fake that tracks calls."""
    calls = []
    fake = type("FakeK32", (), {})()

    def fake_create(*args, **kwargs):
        calls.append(("CreateFileW", args))
        return ctypes.c_void_p(42)

    def fake_write(handle, buf, size, written, overlapped):
        written._obj = size
        calls.append(("WriteFile", handle, size))
        return 1

    def fake_read(handle, buf, size, read, overlapped):
        read._obj = size
        calls.append(("ReadFile", handle, size))
        return 1

    def fake_flush(handle):
        calls.append(("FlushFileBuffers", handle))
        return 1

    def fake_close(handle):
        calls.append(("CloseHandle", handle))
        return 1

    def fake_ioctl(handle, code, inbuf, insize, outbuf, outsize, returned, overlapped):
        if outbuf is not None and outsize >= 8:
            ctypes.memmove(outbuf, ctypes.c_ulonglong(10_000_000_000).value.to_bytes(8, "little"), 8)
        returned._obj = 8
        calls.append(("DeviceIoControl", handle, code))
        return 1

    import core.deviceio as deviceio_mod
    monkeypatch.setattr(deviceio_mod, "kernel32", lambda: fake)
    fake.CreateFileW = fake_create
    fake.WriteFile = fake_write
    fake.ReadFile = fake_read
    fake.FlushFileBuffers = fake_flush
    fake.CloseHandle = fake_close
    fake.DeviceIoControl = fake_ioctl
    fake.GetLastError = lambda: 0
    return calls, fake


def test_benchmark_write_returns_positive(monkeypatch):
    _fake_kernel32(monkeypatch)
    result = benchmark_write("\\\\.\\E:", size=1024, chunk=512)
    assert isinstance(result, float)
    assert result >= 0.0


def test_benchmark_read_returns_positive(monkeypatch):
    _fake_kernel32(monkeypatch)
    result = benchmark_read("\\\\.\\E:", size=1024, chunk=512)
    assert isinstance(result, float)
    assert result >= 0.0


def test_estimate_write_time_returns_positive(monkeypatch):
    _fake_kernel32(monkeypatch)
    result = estimate_write_time("\\\\.\\E:", image_size=10_000_000_000)
    assert isinstance(result, float)
    assert result >= 0.0
