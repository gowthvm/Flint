"""Clone worker tests: sector-by-sector passthrough, target-size guard,
cancellation and error paths (no real hardware)."""

import ctypes
import itertools

from core.clone import CloneWorker


class _FakeCopy:
    def __init__(self, payload: bytes, target_size: int | None = None) -> None:
        self.payload = payload
        self.target_size = target_size if target_size is not None else len(payload)
        self.read_offset = 0
        self.target_read_offset = 0
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

    def _read_target_chunk(self, handle, count: int) -> bytes:
        payload = b"".join(self.written)
        chunk = payload[
            self.target_read_offset : self.target_read_offset + count
        ]
        self.target_read_offset += len(chunk)
        return chunk

    def _seek(self, handle, offset: int) -> None:
        if int(getattr(handle, "value", handle)) == 2001:
            self.read_offset = offset
        else:
            self.target_read_offset = offset


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
    worker._read_target_chunk = fake._read_target_chunk  # type: ignore[method-assign]
    worker._seek = fake._seek  # type: ignore[method-assign]
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


def test_clone_copies_source_to_target(monkeypatch):
    payload = bytes(range(256)) * 400 + b"more-tail"  # ~102 KB
    fake = _FakeCopy(payload)
    worker = _make_worker(fake)
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"] == [(True, "")]
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

    assert events["done"] == [(False, "target drive is smaller than the source")]
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

    assert events["done"] == [(False, "cancelled")]
    assert len(b"".join(fake.written)) < len(payload)


def test_clone_reports_source_read_failure(monkeypatch):
    payload = b"\x33" * (150 * 1024)
    fake = _FakeCopy(payload)
    fake.fail_at = 64 * 1024
    worker = _make_worker(fake)
    closed = _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"] == [(False, "simulated source read failure")]
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

    assert events["done"] == [(False, r"could not open \\.\PHYSICALDRIVE6 for write")]
    assert 2001 in closed


def test_clone_reports_readback_mismatch(monkeypatch):
    payload = b"source-data" * 10_000
    fake = _FakeCopy(payload)
    worker = _make_worker(fake)
    closed = _patch_kernel(monkeypatch)

    original_readback = fake._read_target_chunk

    def corrupt_readback(handle, count):
        data = bytearray(original_readback(handle, count))
        if data:
            data[0] ^= 0xFF
        return bytes(data)

    worker._read_target_chunk = corrupt_readback  # type: ignore[method-assign]
    events = _run(worker)

    assert events["done"][0][0] is False
    assert "clone verification failed at byte 0" in events["done"][0][1]
    assert 2001 in closed and 2002 in closed


def test_clone_opens_letterless_drives_exclusively(monkeypatch):
    """B03: letterless source/target are opened with exclusive=True (the
    handle itself is the lock); with letters the volume locks do it and the
    handles stay shareable."""
    import core.clone as clone_mod

    calls: list[dict] = []

    def fake_open(path, *, write, flags=0, exclusive=False):
        calls.append({"path": path, "write": write, "exclusive": exclusive})
        return ctypes.c_void_p(2001)

    monkeypatch.setattr(clone_mod, "open_drive", fake_open)

    letterless = CloneWorker(r"\\.\PHYSICALDRIVE5", r"\\.\PHYSICALDRIVE6")
    letterless._open_source()
    letterless._open_target()
    assert calls[-2] == {
        "path": r"\\.\PHYSICALDRIVE5",
        "write": False,
        "exclusive": True,
    }
    assert calls[-1] == {
        "path": r"\\.\PHYSICALDRIVE6",
        "write": True,
        "exclusive": True,
    }

    lettered = CloneWorker(
        r"\\.\PHYSICALDRIVE5",
        r"\\.\PHYSICALDRIVE6",
        source_letters=["F"],
        target_letters=["G"],
    )
    lettered._open_source()
    lettered._open_target()
    assert calls[-2]["exclusive"] is False
    assert calls[-1]["exclusive"] is False


def test_clone_zero_fills_tail_and_verifies_full_target(monkeypatch):
    """B08: a larger target has its tail zero-filled and the tail verified;
    L17: progress never restarts (monotonic, nothing re-emitted in verify)."""
    payload = bytes(range(256)) * 400  # 102 400 bytes
    tail = 30 * 1024
    fake = _FakeCopy(payload, target_size=len(payload) + tail)
    worker = _make_worker(fake)
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"] == [(True, "")]
    assert b"".join(fake.written) == payload + b"\x00" * tail
    assert events["total_bytes"][-1] == (len(payload) + tail,)
    assert events["written_bytes"][-1] == (len(payload) + tail,)

    progress = [value for (value,) in events["progress"]]
    assert progress[-1] == 100.0
    assert all(
        later >= earlier for earlier, later in itertools.pairwise(progress)
    ), "progress must stay monotonic across clone/zero-fill/verify"
    written = [value for (value,) in events["written_bytes"]]
    assert all(
        later >= earlier for earlier, later in itertools.pairwise(written)
    )

    phases = [value for (value,) in events["phase"]]
    assert "Zero-filling" in phases
    assert "Verifying clone" in phases

    # L17: once verification starts, progress must not be emitted again.
    order: list[tuple[str, object]] = []
    worker2 = _make_worker(_FakeCopy(payload, target_size=len(payload) + tail))
    _patch_kernel(monkeypatch)
    for name in ("progress", "phase"):
        getattr(worker2, name).connect(
            lambda *args, _n=name: order.append((_n, args))
        )
    worker2.run()
    verify_at = next(
        i for i, (kind, args) in enumerate(order)
        if kind == "phase" and args and args[0] == "Verifying clone"
    )
    assert not any(kind == "progress" for kind, _ in order[verify_at:])


def test_clone_zero_tail_can_be_disabled(monkeypatch):
    """zero_tail=False keeps the old contract: only source_size bytes are
    written and only that range is verified."""
    payload = b"\x42" * (64 * 1024)
    fake = _FakeCopy(payload, target_size=len(payload) + 16 * 1024)

    def make():
        worker = _make_worker(fake)
        worker.zero_tail = False
        return worker

    worker = make()
    _patch_kernel(monkeypatch)

    events = _run(worker)

    assert events["done"] == [(True, "")]
    assert b"".join(fake.written) == payload
    assert events["total_bytes"][-1] == (len(payload),)


def test_clone_verification_detects_stale_tail_bytes(monkeypatch):
    """B08: if the tail is not all zeros the full-target verification fails
    at the offending byte."""
    payload = bytes(range(256)) * 100  # 25 600 bytes, contains no all-zero chunk
    tail = 8 * 1024
    fake = _FakeCopy(payload, target_size=len(payload) + tail)
    worker = _make_worker(fake)
    _patch_kernel(monkeypatch)

    original_write = fake._write_chunk
    written = bytearray()

    def dirty_tail(handle, data: bytes) -> None:
        written.extend(data)
        if len(written) > len(payload):
            buf = bytearray(data)
            buf[0] = 0x5A
            original_write(handle, bytes(buf))
        else:
            original_write(handle, data)

    worker._write_chunk = dirty_tail  # type: ignore[method-assign]
    events = _run(worker)

    assert events["done"][0][0] is False
    assert (
        f"clone verification failed at byte {len(payload):,}"
        in events["done"][0][1]
    )


def test_clone_cancel_during_zero_fill(monkeypatch):
    """Cancellation between the copy and the tail is honored: no flush, no
    verify, one "cancelled" result."""
    payload = b"\x11" * (100 * 1024)
    fake = _FakeCopy(payload, target_size=len(payload) + 32 * 1024)
    worker = _make_worker(fake)
    _patch_kernel(monkeypatch)

    original_write = worker._write_chunk
    written = bytearray()

    def cancel_on_tail(handle, data: bytes) -> None:
        if len(written) >= len(payload):
            worker.cancel()
            return
        written.extend(data)
        return original_write(handle, data)

    worker._write_chunk = cancel_on_tail  # type: ignore[method-assign]
    events = _run(worker)

    assert events["done"] == [(False, "cancelled")]
    assert len(written) == len(payload)


def test_clone_emits_finished_once_when_unlock_raises(monkeypatch):
    """L04: run()'s outer except must not re-emit after _run_inner already
    reported success."""
    payload = b"\x33" * (16 * 1024)
    fake = _FakeCopy(payload)
    worker = _make_worker(fake)
    _patch_kernel(monkeypatch)

    def angry_unlock(held) -> None:
        raise OSError("volume unlock failed")

    worker._unlock_volumes = angry_unlock  # type: ignore[method-assign]
    events = _run(worker)

    assert events["done"] == [(True, "")]