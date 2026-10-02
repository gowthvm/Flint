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
        "done",
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

    assert events["done"] == [(True, "")]
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
    worker._canceled = False

    def ticking_cancel() -> None:
        if fake.offset >= 70 * 1024:
            worker.cancel()

    original = worker._read_chunk
    worker._read_chunk = (  # type: ignore[method-assign]
        lambda handle, count: (ticking_cancel(), original(handle, count))[1]
    )

    events = _run(worker)

    assert events["done"] == [(False, "cancelled")]
    assert not out.exists(), "partial file should be removed on cancel"
    assert not events["digest"], "no digest reported for a cancelled backup"


def test_backup_read_failure_reported(tmp_path, monkeypatch):
    payload = b"\x00" * (150 * 1024)
    fake = _FakeReads(payload)
    fake.fail_at = 64 * 1024
    out = tmp_path / "backup.img"
    worker = _make_worker(fake, out)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"] == [(False, "simulated read failure")]


def test_backup_reports_open_failure(tmp_path):
    worker = BackupWorker(r"\\.\PHYSICALDRIVE8", str(tmp_path / "x.img"))

    def fail_open():
        raise OSError(r"could not open \\.\PHYSICALDRIVE8 for read")

    worker._open_drive = fail_open  # type: ignore[method-assign]
    events: list = []
    worker.done.connect(lambda ok, msg: events.append((ok, msg)))

    worker.run()

    assert events == [(False, r"could not open \\.\PHYSICALDRIVE8 for read")]


def test_backup_never_flushes_readonly_drive_handle(tmp_path, monkeypatch):
    """The drive handle is GENERIC_READ only, where FlushFileBuffers fails
    with ERROR_ACCESS_DENIED; a real backup must not call it at all."""
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

    assert events["done"] == [(True, "")]
    assert flushed == []
    assert out.read_bytes() == payload

def test_backup_opens_letterless_drive_exclusively(tmp_path, monkeypatch):
    """B03: a drive with no mounted letters must be opened with
    exclusive=True (the handle itself is the lock); with letters the handle
    stays shareable because the volume locks do the locking."""
    import core.backup as backup_mod

    calls: list[dict] = []

    def fake_open(path, *, write, flags=0, exclusive=False):
        calls.append({"path": path, "write": write, "exclusive": exclusive})
        return ctypes.c_void_p(1001)

    monkeypatch.setattr(backup_mod, "open_drive", fake_open)

    letterless = BackupWorker(r"\\.\PHYSICALDRIVE8", str(tmp_path / "a.img"))
    letterless._open_drive()
    assert calls[-1]["exclusive"] is True
    assert calls[-1]["write"] is False

    lettered = BackupWorker(
        r"\\.\PHYSICALDRIVE8", str(tmp_path / "b.img"), letters=["E"]
    )
    lettered._open_drive()
    assert calls[-1]["exclusive"] is False


def test_backup_success_replaces_existing_file(tmp_path, monkeypatch):
    """B07: a completed backup publishes the image atomically over any
    file already at out_path, leaving no .partial behind."""
    payload = b"\x77" * (64 * 1024 + 3)
    out = tmp_path / "backup.img"
    out.write_bytes(b"previous image contents")
    fake = _FakeReads(payload)
    worker = _make_worker(fake, out)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"] == [(True, "")]
    assert out.read_bytes() == payload
    assert not (tmp_path / "backup.img.partial").exists()
    assert not list(tmp_path.glob("*.partial"))


def test_backup_failure_preserves_existing_file(tmp_path, monkeypatch):
    """B07: the destination is never truncated up-front — a failure mid-way
    removes only the .partial file and leaves out_path untouched."""
    payload = b"\x5a" * (200 * 1024)
    out = tmp_path / "backup.img"
    previous = b"do not destroy me" * 100
    out.write_bytes(previous)
    fake = _FakeReads(payload)
    fake.fail_at = 64 * 1024
    worker = _make_worker(fake, out)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"] == [(False, "simulated read failure")]
    assert out.read_bytes() == previous
    assert not list(tmp_path.glob("*.partial"))


def test_backup_free_space_checked_before_touching_destination(
    tmp_path, monkeypatch
):
    """B07: insufficient free space on the destination fails before any
    file is created or modified."""
    payload = b"\x00" * (128 * 1024)
    out = tmp_path / "backup.img"
    previous = b"keep this file"
    out.write_bytes(previous)
    fake = _FakeReads(payload)
    worker = _make_worker(fake, out)
    worker._free_space = lambda directory: 4096  # type: ignore[method-assign]
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"][0][0] is False
    assert "not enough free space" in events["done"][0][1]
    assert out.read_bytes() == previous
    assert not list(tmp_path.glob("*.partial"))
    assert fake.offset == 0, "no source bytes should be read before the check"


def test_backup_emits_finished_once_when_unlock_raises(tmp_path, monkeypatch):
    """L04: run()'s outer except must not re-emit after _run_inner already
    reported success."""
    payload = b"\x33" * (16 * 1024)
    fake = _FakeReads(payload)
    out = tmp_path / "backup.img"
    worker = _make_worker(fake, out)
    _patch_kernel(monkeypatch)

    def angry_unlock(held) -> None:
        raise OSError("volume unlock failed")

    worker.unlock_volumes = angry_unlock  # type: ignore[method-assign]

    events = _run(worker)

    assert events["done"] == [(True, "")]