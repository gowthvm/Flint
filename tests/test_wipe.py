"""Wipe worker tests: the zero-fill loop, cancellation, error paths and
handle lifecycle.

The wipe loop is exercised without real hardware by swapping the worker's
kernel access points (open/size/write/lock) for fakes; CloseHandle and
FlushFileBuffers are patched on the win32 kernel object so no real device
is touched.
"""

import ctypes

from core.wipe import WipeWorker


class _FakeKernel:
    def __init__(self, size: int) -> None:
        self.size = size
        self.chunks: list[bytes] = []
        self.fail_after: int | None = None
        # Models the physical disk being rewritten in place.
        self.data = bytearray(size)
        self.pos = 0
        self.fail_read = False

    def _open_drive(self):
        return ctypes.c_void_p(1234)

    def _drive_size(self, handle) -> int:
        return self.size

    def _lock_volumes(self) -> list:
        return []

    def _unlock_volumes(self, held) -> None:
        pass

    def _write_chunk(self, handle, data: bytes) -> None:
        if self.fail_after is not None and len(self.chunks) >= self.fail_after:
            raise OSError("simulated write failure")
        self.chunks.append(data)
        # The OS auto-advances the file pointer on real disk writes.
        end = min(self.pos + len(data), len(self.data))
        self.data[self.pos:end] = data[: end - self.pos]
        self.pos += len(data)

    def _seek_start(self, handle) -> None:
        self.pos = 0

    def _read_chunk(self, handle, size: int) -> bytes:
        if self.fail_read:
            raise OSError("simulated read failure")
        chunk = bytes(self.data[self.pos : self.pos + size])
        self.pos += size
        return chunk


def _make_worker(
    fake: _FakeKernel,
    cancel_after: int | None = None,
    method: str = "zero",
    verify: bool = False,
) -> WipeWorker:
    worker = WipeWorker(r"\\.\PHYSICALDRIVE9", method=method, verify=verify)
    worker._open_drive = fake._open_drive  # type: ignore[method-assign]
    worker._drive_size = fake._drive_size  # type: ignore[method-assign]
    worker._lock_volumes = fake._lock_volumes  # type: ignore[method-assign]
    worker._unlock_volumes = fake._unlock_volumes  # type: ignore[method-assign]
    worker._seek_start = fake._seek_start  # type: ignore[method-assign]
    worker._read_chunk = fake._read_chunk  # type: ignore[method-assign]
    if cancel_after is not None:
        real_write = fake._write_chunk

        def counting_write(handle, data: bytes) -> None:
            real_write(handle, data)
            if len(fake.chunks) >= cancel_after:
                worker.cancel()

        worker._write_chunk = counting_write  # type: ignore[method-assign]
    else:
        worker._write_chunk = fake._write_chunk  # type: ignore[method-assign]
    return worker


def _run(worker: WipeWorker) -> dict[str, list]:
    events: dict[str, list] = {}
    for name in (
        "progress",
        "speed_mbps",
        "written_bytes",
        "total_bytes",
        "eta_seconds",
        "phase",
        "verified",
        "finished",
    ):
        events[name] = []

        def _record(*args, _name=name) -> None:
            events[_name].append(args)

        getattr(worker, name).connect(_record)
    worker.run()
    return events


def _patch_kernel(monkeypatch) -> list[int]:
    closed: list[int] = []

    def _record_close(h) -> int:
        closed.append(int(getattr(h, "value", h)))
        return 1

    kernel32 = ctypes.windll.kernel32
    monkeypatch.setattr(kernel32, "CloseHandle", _record_close)
    monkeypatch.setattr(kernel32, "FlushFileBuffers", lambda h: 1)
    return closed


def test_wipe_zero_fills_entire_drive(monkeypatch):
    size = 10 * 1024 * 1024 + 123  # not a multiple of the chunk size
    fake = _FakeKernel(size)
    worker = _make_worker(fake)
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert fake.chunks
    assert all(chunk == b"\x00" * len(chunk) for chunk in fake.chunks)
    assert sum(len(c) for c in fake.chunks) == size
    assert events["written_bytes"][-1] == (size,)
    assert events["total_bytes"][-1] == (size,)
    assert events["progress"][-1][0] == 100.0
    assert 1234 in closed


def test_wipe_cancel_midway(monkeypatch):
    fake = _FakeKernel(20 * 1024 * 1024)
    worker = _make_worker(fake, cancel_after=2)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(False, "cancelled")]
    assert len(fake.chunks) == 2
    assert sum(len(c) for c in fake.chunks) < fake.size


def test_wipe_reports_write_failure_and_closes_handle(monkeypatch):
    fake = _FakeKernel(20 * 1024 * 1024)
    fake.fail_after = 1
    worker = _make_worker(fake)
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(False, "simulated write failure")]
    assert 1234 in closed


def test_wipe_flushes_after_last_chunk(monkeypatch):
    fake = _FakeKernel(3 * 1024 * 1024)
    worker = _make_worker(fake)
    flushed: list[int] = []

    def _record_flush(h) -> int:
        flushed.append(int(getattr(h, "value", h)))
        return 1

    kernel32 = ctypes.windll.kernel32
    monkeypatch.setattr(kernel32, "FlushFileBuffers", _record_flush)
    monkeypatch.setattr(kernel32, "CloseHandle", lambda h: 1)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert flushed == [1234]


def test_wipe_reports_open_failure_without_touching_kernel(monkeypatch):
    worker = WipeWorker(r"\\.\PHYSICALDRIVE9")

    def fail_open():
        raise OSError(r"drive not writable: \\.\PHYSICALDRIVE9")

    worker._open_drive = fail_open  # type: ignore[method-assign]
    events: list = []
    worker.finished.connect(lambda ok, msg: events.append((ok, msg)))

    worker.run()

    assert events == [(False, r"drive not writable: \\.\PHYSICALDRIVE9")]


def test_wipe_random_pass_writes_different_random_chunks(monkeypatch):
    size = 6 * 1024 * 1024
    fake = _FakeKernel(size)
    worker = _make_worker(fake, method="random")
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert sum(len(c) for c in fake.chunks) == size
    assert any(chunk != b"\x00" * len(chunk) for chunk in fake.chunks)
    assert any(chunk != b"\xff" * len(chunk) for chunk in fake.chunks)
    assert events["total_bytes"][-1] == (size,)


def test_wipe_nist_is_a_single_random_pass(monkeypatch):
    fake = _FakeKernel(3 * 1024 * 1024)
    worker = _make_worker(fake, method="nist")
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert events["total_bytes"][-1] == (fake.size,)


def test_wipe_dod_three_passes_zero_ones_random(monkeypatch):
    size = 5 * 1024 * 1024
    fake = _FakeKernel(size)
    worker = _make_worker(fake, method="dod")
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    zero_chunks = [c for c in fake.chunks if c == b"\x00" * len(c)]
    one_chunks = [c for c in fake.chunks if c == b"\xff" * len(c)]
    random_chunks = [c for c in fake.chunks if c != b"\x00" * len(c) and c != b"\xff" * len(c)]
    assert sum(len(c) for c in zero_chunks) == size
    assert sum(len(c) for c in one_chunks) == size
    assert sum(len(c) for c in random_chunks) == size
    # Three full passes are reported to the UI
    assert events["total_bytes"][-1] == (size * 3,)
    assert events["progress"][-1][0] == 100.0
    assert any("pass 1/3" in p[0] for p in events["phase"])
    assert any("pass 3/3" in p[0] for p in events["phase"])


def test_wipe_cancel_inside_second_dod_pass(monkeypatch):
    fake = _FakeKernel(20 * 1024 * 1024)
    worker = _make_worker(fake, cancel_after=2, method="dod")
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(False, "cancelled")]
    assert sum(len(c) for c in fake.chunks) < 2 * fake.size


def test_wipe_unknown_method_rejected_eagerly():
    try:
        WipeWorker(r"\\.\PHYSICALDRIVE9", method="nuclear")
    except ValueError as exc:
        assert "unknown wipe method" in str(exc)
    else:
        raise AssertionError("unknown method must raise at construction")


class _StubbornKernel(_FakeKernel):
    """A drive that always reads back zeros, whatever is written to it."""

    def _read_chunk(self, handle, size: int) -> bytes:
        return b"\x00" * size


def test_wipe_skips_verification_when_disabled(monkeypatch):
    fake = _FakeKernel(3 * 1024 * 1024)
    worker = _make_worker(fake, verify=False)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert events["verified"] == [(True, "skipped")]
    # A single pass, no extra read-back total.
    assert events["total_bytes"][-1] == (fake.size,)


def test_wipe_verifies_zero_fill(monkeypatch):
    size = 5 * 1024 * 1024 + 99
    fake = _FakeKernel(size)
    worker = _make_worker(fake, verify=True)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert events["verified"] == [(True, "zeros confirmed")]
    # Written pass plus a verification read of the whole drive.
    assert events["total_bytes"][-1] == (size + size,)
    assert events["progress"][-1][0] == 100.0
    assert fake.data == bytes(size)


def test_wipe_verifies_random_pass_round_trip(monkeypatch):
    size = 6 * 1024 * 1024
    fake = _FakeKernel(size)
    worker = _make_worker(fake, method="random", verify=True)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert events["verified"] == [(True, "random pattern confirmed")]
    assert events["total_bytes"][-1] == (2 * size,)
    assert any("Verifying" in p[0] for p in events["phase"])


def test_wipe_verification_reports_mismatch(monkeypatch):
    size = 4 * 1024 * 1024
    fake = _StubbornKernel(size)
    worker = _make_worker(fake, method="random", verify=True)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["verified"] == [
        (False, "data mismatch at offset 0")
    ]
    assert events["finished"] == [
        (False, "verification failed: data mismatch at offset 0")
    ]


def test_wipe_verification_reports_read_failure(monkeypatch):
    fake = _FakeKernel(4 * 1024 * 1024)
    fake.fail_read = True
    worker = _make_worker(fake, method="random", verify=True)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["verified"] == [(False, "read error: simulated read failure")]
    assert events["finished"] == [(False, "simulated read failure")]
