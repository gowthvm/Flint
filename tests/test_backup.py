"""Backup worker tests: streaming read to a file, digest, cancellation,
error paths and handle lifecycle (no real hardware)."""

import ctypes
import hashlib

from core.backup import BackupWorker


class _FakeReads:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.fail_at: int | None = None

    def _open_drive(self):
        return ctypes.c_void_p(1001)

    def _drive_size(self, handle) -> int:
        return len(self.payload)

    def _lock_volumes(self) -> list:
        return []

    def _unlock_volumes(self, held) -> None:
        pass

    def _read_chunk(self, handle, count: int) -> bytes:
        if self.fail_at is not None and self.offset >= self.fail_at:
            raise OSError("simulated read failure")
        chunk = self.payload[self.offset : self.offset + count]
        self.offset += len(chunk)
        return chunk


def _make_worker(fake, out_path) -> BackupWorker:
    worker = BackupWorker(r"\\.\PHYSICALDRIVE8", str(out_path))
    worker._open_drive = fake._open_drive  # type: ignore[method-assign]
    worker._drive_size = fake._drive_size  # type: ignore[method-assign]
    worker._lock_volumes = fake._lock_volumes  # type: ignore[method-assign]
    worker._unlock_volumes = fake._unlock_volumes  # type: ignore[method-assign]
    worker._read_chunk = fake._read_chunk  # type: ignore[method-assign]
    worker.CHUNK_SIZE = 64 * 1024
    return worker


def _run(worker: BackupWorker) -> dict[str, list]:
    events: dict[str, list] = {}
    for name in (
        "progress",
        "speed_mbps",
        "written_bytes",
        "total_bytes",
        "eta_seconds",
        "phase",
        "digest",
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


def test_backup_streams_drive_to_file(tmp_path, monkeypatch):
    payload = bytes(range(256)) * 500 + b"tail-bytes"  # 128 KB + tail
    fake = _FakeReads(payload)
    out = tmp_path / "backup.img"
    worker = _make_worker(fake, out)
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert out.read_bytes() == payload
    assert events["written_bytes"][-1] == (len(payload),)
    assert events["total_bytes"][-1] == (len(payload),)
    assert events["progress"][-1][0] == 100.0
    assert events["digest"][-1] == (hashlib.sha256(payload).hexdigest(),)
    assert 1001 in closed


def test_backup_cancel_midway(tmp_path, monkeypatch):
    payload = b"\xaa" * (200 * 1024)
    fake = _FakeReads(payload)
    out = tmp_path / "backup.img"
    worker = _make_worker(fake, out)
    _patch_kernel(monkeypatch)
    worker._cancelled = False

    def ticking_cancel() -> None:
        if fake.offset >= 70 * 1024:
            worker.cancel()

    original = worker._read_chunk
    worker._read_chunk = (  # type: ignore[method-assign]
        lambda handle, count: (ticking_cancel(), original(handle, count))[1]
    )

    events = _run(worker)

    assert events["finished"] == [(False, "cancelled")]
    assert len(out.read_bytes()) < len(payload)
    assert not events["digest"], "no digest reported for a cancelled backup"


def test_backup_read_failure_reported(tmp_path, monkeypatch):
    payload = b"\x00" * (150 * 1024)
    fake = _FakeReads(payload)
    fake.fail_at = 64 * 1024
    out = tmp_path / "backup.img"
    worker = _make_worker(fake, out)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["finished"] == [(False, "simulated read failure")]


def test_backup_reports_open_failure(tmp_path):
    worker = BackupWorker(r"\\.\PHYSICALDRIVE8", str(tmp_path / "x.img"))

    def fail_open():
        raise OSError(r"could not open \\.\PHYSICALDRIVE8 for read")

    worker._open_drive = fail_open  # type: ignore[method-assign]
    events: list = []
    worker.finished.connect(lambda ok, msg: events.append((ok, msg)))

    worker.run()

    assert events == [(False, r"could not open \\.\PHYSICALDRIVE8 for read")]


def test_backup_flushes_and_closes_after_last_chunk(tmp_path, monkeypatch):
    payload = b"\x01" * (64 * 1024 + 7)
    fake = _FakeReads(payload)
    out = tmp_path / "backup.img"
    worker = _make_worker(fake, out)
    flushed: list[int] = []

    def _record_flush(h) -> int:
        flushed.append(int(getattr(h, "value", h)))
        return 1

    kernel32 = ctypes.windll.kernel32
    monkeypatch.setattr(kernel32, "FlushFileBuffers", _record_flush)
    monkeypatch.setattr(kernel32, "CloseHandle", lambda h: 1)

    events = _run(worker)

    assert events["finished"] == [(True, "")]
    assert flushed == [1001]