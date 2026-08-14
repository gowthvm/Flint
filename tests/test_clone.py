"""Clone worker tests: sector-by-sector passthrough, target-size guard,
cancellation and error paths (no real hardware)."""

import ctypes

from core.clone import CloneWorker


class _FakeCopy:
    def __init__(self, payload: bytes, target_size: int | None = None) -> None:
        self.payload = payload
        self.target_size = target_size if target_size is not None else len(payload)
        self.read_offset = 0
        self.written: list[bytes] = []
        self.fail_at: int | None = None

    def _open_source(self):
        return ctypes.c_void_p(2001)

    def _open_target(self):
        return ctypes.c_void_p(2002)

    def _source_size(self, handle) -> int:
        return len(self.payload)

    def _target_size(self, handle) -> int:
        return self.target_size

    def _lock_volumes(self) -> list:
        return []

    def _unlock_volumes(self, held) -> None:
        pass

    def _read_chunk(self, handle, count: int) -> bytes:
        if self.fail_at is not None and self.read_offset >= self.fail_at:
            raise OSError("simulated source read failure")
        chunk = self.payload[self.read_offset : self.read_offset + count]
        self.read_offset += len(chunk)
        return chunk

    def _write_chunk(self, handle, data: bytes) -> None:
        self.written.append(data)


def _make_worker(fake, target_size: int | None = None) -> CloneWorker:
    worker = CloneWorker(
        r"\\.\PHYSICALDRIVE5",
        r"\\.\PHYSICALDRIVE6",
        source_letters=["F"],
        target_letters=["G"],
    )
    if target_size is not None:
        fake.target_size = target_size
    worker._open_source = fake._open_source  # type: ignore[method-assign]
    worker._open_target = fake._open_target  # type: ignore[method-assign]
    worker._source_size = fake._source_size  # type: ignore[method-assign]
    worker._target_size = fake._target_size  # type: ignore[method-assign]
    worker._lock_volumes = fake._lock_volumes  # type: ignore[method-assign]
    worker._unlock_volumes = fake._unlock_volumes  # type: ignore[method-assign]
    worker._read_chunk = fake._read_chunk  # type: ignore[method-assign]
    worker._write_chunk = fake._write_chunk  # type: ignore[method-assign]
    worker.CHUNK_SIZE = 64 * 1024
    return worker


def _run(worker: CloneWorker) -> dict[str, list]:
    events: dict[str, list] = {}
    for name in (
        "progress",
        "speed_mbps",
        "written_bytes",
        "total_bytes",
        "eta_seconds",
        "phase",
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


def test_clone_copies_source_to_target(monkeypatch):
    payload = bytes(range(256)) * 400 + b"more-tail"  # ~102 KB
    fake = _FakeCopy(payload)
    worker = _make_worker(fake)
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert b"".join(fake.written) == payload
    assert events["written_bytes"][-1] == (len(payload),)
    assert events["total_bytes"][-1] == (len(payload),)
    assert events["progress"][-1][0] == 100.0
    assert 2001 in closed and 2002 in closed


def test_clone_refuses_smaller_target_without_writing(monkeypatch):
    payload = b"\x11" * (100 * 1024)
    fake = _FakeCopy(payload, target_size=50 * 1024)
    worker = _make_worker(fake)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(False, "target drive is smaller than the source")]
    assert fake.written == []


def test_clone_cancel_midway(monkeypatch):
    payload = b"\x22" * (200 * 1024)
    fake = _FakeCopy(payload)
    worker = _make_worker(fake)
    _patch_kernel(monkeypatch)

    original_read = worker._read_chunk

    def cancel_then_read(handle, count: int):
        if fake.read_offset >= 80 * 1024:
            worker.cancel()
        return original_read(handle, count)

    worker._read_chunk = cancel_then_read  # type: ignore[method-assign]
    events = _run(worker)

    assert events["finished"] == [(False, "cancelled")]
    assert len(b"".join(fake.written)) < len(payload)


def test_clone_reports_source_read_failure(monkeypatch):
    payload = b"\x33" * (150 * 1024)
    fake = _FakeCopy(payload)
    fake.fail_at = 64 * 1024
    worker = _make_worker(fake)
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(False, "simulated source read failure")]
    assert 2001 in closed and 2002 in closed


def test_clone_reports_target_open_failure(monkeypatch):
    payload = b"\x44" * (10 * 1024)
    fake = _FakeCopy(payload)

    def fail_target():
        raise OSError(r"could not open \\.\PHYSICALDRIVE6 for write")

    worker = _make_worker(fake)
    worker._open_target = fail_target  # type: ignore[method-assign]
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(False, r"could not open \\.\PHYSICALDRIVE6 for write")]
    assert 2001 in closed