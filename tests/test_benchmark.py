"""Tests for core.benchmark — read/write benchmarks.

D1: ``core.benchmark`` imports ``kernel32`` by value at module import time,
so the fake has to be patched onto ``core.benchmark.kernel32``.  Patching
``core.deviceio.kernel32`` leaves the real Win32 API in place, and the old
``result >= 0.0`` assertions then passed on the ``0.0`` an un-openable
drive returns — a test that both touched real hardware and could never fail.

Every drive path below is a deliberately non-existent ``\\\\.\\`` device, so
a regression fails loudly (CreateFileW -> invalid handle -> 0.0 -> assertion)
instead of writing to whatever volume the host happens to have.
"""

import ctypes

import pytest

from core.benchmark import benchmark_read, benchmark_write, estimate_write_time

# Not a real device: CreateFileW on this always fails.
_FAKE_DRIVE = r"\\.\FlintBenchNoDrive"


def _fake_kernel32(monkeypatch):
    """Install a fake kernel32 on the module that actually calls it."""
    calls = []
    fake = type("FakeK32", (), {})()

    def fake_create(*args, **kwargs):
        calls.append(("CreateFileW", args))
        return ctypes.c_void_p(42)

    def fake_write(handle, buf, size, written, overlapped):
        # `written` is a byref() CArgObject: write through to the pointee's
        # .value, otherwise the caller still reads 0 and stops after one
        # iteration.
        written._obj.value = size
        calls.append(("WriteFile", handle, size))
        return 1

    def fake_read(handle, buf, size, read, overlapped):
        read._obj.value = size
        calls.append(("ReadFile", handle, size))
        return 1

    def fake_close(handle):
        calls.append(("CloseHandle", handle))
        return 1

    fake.CreateFileW = fake_create
    fake.WriteFile = fake_write
    fake.ReadFile = fake_read
    fake.CloseHandle = fake_close
    fake.GetLastError = lambda: 0

    monkeypatch.setattr("core.benchmark.kernel32", lambda: fake)
    return calls, fake


def _opened_path(calls):
    opens = [c for c in calls if c[0] == "CreateFileW"]
    assert len(opens) == 1, f"expected exactly one CreateFileW, got {opens}"
    return opens[0][1][0]


def _total(calls, op):
    return sum(c[2] for c in calls if c[0] == op)


def test_benchmark_write_requires_confirm(monkeypatch):
    """B12: writing random data over the start of a raw device must be
    impossible to trigger by accident."""
    calls, _ = _fake_kernel32(monkeypatch)

    with pytest.raises(ValueError, match="would overwrite the first"):
        benchmark_write(_FAKE_DRIVE, size=1024, chunk=512)

    assert calls == [], "the guard must fire before the device is opened"


def test_benchmark_write_confirmed_runs(monkeypatch):
    calls, _ = _fake_kernel32(monkeypatch)
    result = benchmark_write(_FAKE_DRIVE, size=1024, chunk=512, confirm=True)

    assert isinstance(result, float)
    # 0.0 is exactly what an un-patched run returns, so a vacuous pass
    # would mean the fake never took over.
    assert result > 0.0
    assert _opened_path(calls) == _FAKE_DRIVE
    assert _total(calls, "WriteFile") == 1024
    assert calls[-1][0] == "CloseHandle", "the handle must always be closed"


def test_benchmark_write_reports_zero_when_the_device_cannot_open(monkeypatch):
    """An un-openable device must not raise and must not report a speed."""
    calls, fake = _fake_kernel32(monkeypatch)
    fake.CreateFileW = lambda *a, **k: ctypes.c_void_p(-1).value
    monkeypatch.setattr("core.benchmark.kernel32", lambda: fake)

    result = benchmark_write(_FAKE_DRIVE, size=1024, chunk=512, confirm=True)

    assert result == 0.0
    assert calls == []


def test_benchmark_read_returns_positive(monkeypatch):
    calls, _ = _fake_kernel32(monkeypatch)
    result = benchmark_read(_FAKE_DRIVE, size=1024, chunk=512)

    assert isinstance(result, float)
    assert result > 0.0
    assert _opened_path(calls) == _FAKE_DRIVE
    assert _total(calls, "ReadFile") == 1024
    assert calls[-1][0] == "CloseHandle"


def test_estimate_write_time_requires_confirm(monkeypatch):
    """estimate_write_time runs a destructive benchmark_write under the
    hood, so it carries the same guard."""
    calls, _ = _fake_kernel32(monkeypatch)

    with pytest.raises(ValueError, match="would overwrite the first"):
        estimate_write_time(_FAKE_DRIVE, image_size=10_000_000_000)

    assert calls == [], "the guard must fire before the device is opened"

    result = estimate_write_time(
        _FAKE_DRIVE, image_size=10_000_000_000, confirm=True
    )
    assert isinstance(result, float)
    assert result > 0.0
    assert _opened_path(calls) == _FAKE_DRIVE
    # 2 MiB quick benchmark, then the image size scaled off that speed.
    assert _total(calls, "WriteFile") == 2 * 1024 * 1024
    assert result > 0.0
    assert calls[-1][0] == "CloseHandle"
