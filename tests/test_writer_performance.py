"""Performance-related tests: chunking logic, native-writer dispatch and a
micro-benchmark of the Python vs. the compiled native write path.

The native extension (``core._native_writer``) is optional: every native test
is skipped when it has not been built (``python setup.py build_ext --inplace``).
"""

import ctypes
import logging
import math
import os
import sys
import time

import pytest

from core import writer
from core.wipe import WipeWorker

logger = logging.getLogger(__name__)


def _native_available() -> bool:
    try:
        import core._native_writer  # noqa: F401

        return True
    except ImportError:
        return False


requires_native = pytest.mark.skipif(
    not _native_available(), reason="native writer extension not built"
)


def _blob(size: int, seed: int = 0) -> bytes:
    data = bytearray(size)
    for i in range(0, size, 64):
        data[i : i + 64] = bytes((seed + i // 64) % 256 for _ in range(64))
    return bytes(data)


# ---------------------------------------------------------------- chunking --


def test_python_write_stream_roundtrip(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(1_000_000, seed=7)
    src.write_bytes(payload)

    written = writer.write_stream(str(src), str(dst), chunk_size=256 * 1024)

    assert written == len(payload)
    assert dst.read_bytes() == payload


def test_python_write_stream_progress_calls(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(10_000, seed=3)
    src.write_bytes(payload)
    calls: list[tuple[int, int]] = []

    writer.write_stream(
        str(src), str(dst), chunk_size=4096,
        progress=lambda done, total: calls.append((done, total)),
    )

    assert calls[-1] == (len(payload), len(payload))
    assert [done for done, _ in calls] == sorted(d for d, _ in calls)
    assert all(total == len(payload) for _, total in calls)
    # ceil(size / chunk_size) chunk callbacks
    assert len(calls) == math.ceil(len(payload) / 4096)


def test_write_stream_forwards_chunk_size(tmp_path, monkeypatch):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 1024)
    recorded: dict = {}

    def fake_python_stream(s, d, chunk_size, progress=None):
        recorded.update(src=s, device=d, chunk_size=chunk_size)
        return 1024

    monkeypatch.setattr(writer, "_python_write_stream", fake_python_stream)
    result = writer.write_stream(
        str(src), str(tmp_path / "dst.bin"), chunk_size=16 * 1024
    )

    assert result == 1024
    assert recorded["chunk_size"] == 16 * 1024
    assert recorded["src"] == str(src)


def test_write_stream_falls_back_when_native_missing(tmp_path, monkeypatch):
    """A missing/unbuildable extension silently falls back to Python IO."""
    monkeypatch.setitem(sys.modules, "core._native_writer", None)
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(50_000, seed=11)
    src.write_bytes(payload)

    written = writer.write_stream(
        str(src), str(dst), chunk_size=8192, use_native=True
    )

    assert written == len(payload)
    assert dst.read_bytes() == payload


# ---------------------------------------------------------------- native ----


@requires_native
def test_native_write_matches_source(tmp_path):
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(3 * 1024 * 1024, seed=21)
    src.write_bytes(payload)

    written = native.native_write(str(src), str(dst), 1024 * 1024)

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_aligns_odd_chunk_size(tmp_path):
    """Chunk sizes that are not 4096-multiples are aligned inside the C code."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(2_500_000, seed=33)
    src.write_bytes(payload)

    written = native.native_write(str(src), str(dst), 8193)

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_progress_and_trim(tmp_path):
    """Final partial chunk is sector-padded then the file is trimmed back."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(10_001, seed=5)  # one byte over three 4096 chunks
    src.write_bytes(payload)
    calls: list[tuple[int, int]] = []

    written = native.native_write(
        str(src), str(dst), 4096,
        progress=lambda done, total: calls.append((done, total)),
    )

    assert written == len(payload)
    assert dst.read_bytes() == payload
    assert calls[-1] == (len(payload), len(payload))
    assert len(calls) == 3


@requires_native
def test_native_write_missing_source_raises_oserror(tmp_path):
    import core._native_writer as native

    with pytest.raises(OSError):
        native.native_write(
            str(tmp_path / "missing.bin"), str(tmp_path / "dst.bin")
        )


@requires_native
def test_native_write_callback_cancel(tmp_path):
    """Raising from the progress callback aborts the write cleanly."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    src.write_bytes(_blob(2 * 1024 * 1024, seed=9))

    def boom(done, total):
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        native.native_write(str(src), str(tmp_path / "dst.bin"), 4096, boom)


# ----------------------------------- native writer error-path coverage ----


@requires_native
def test_native_write_empty_source(tmp_path):
    """A 0-byte source should return 0 written without crashing."""
    import core._native_writer as native

    src = tmp_path / "empty.bin"
    src.write_bytes(b"")
    dst = tmp_path / "dst.bin"

    written = native.native_write(str(src), str(dst), 4096)

    assert written == 0
    assert dst.exists()
    assert dst.stat().st_size == 0


@requires_native
def test_native_write_destination_open_fails(tmp_path):
    """Writing to a non-existent device path raises OSError."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    src.write_bytes(_blob(4096, seed=1))

    with pytest.raises(OSError):
        native.native_write(str(src), r"\\.\PHYSICALDRIVE99")


@requires_native
def test_native_write_readonly_destination_fails(tmp_path):
    """Writing to a read-only destination raises OSError."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    src.write_bytes(_blob(4096, seed=2))
    dst = tmp_path / "readonly.bin"
    dst.write_bytes(b"\x00" * 4096)
    dst.chmod(0o444)

    try:
        with pytest.raises(OSError):
            native.native_write(str(src), str(dst), 4096)
    finally:
        dst.chmod(0o666)


@requires_native
def test_native_write_chunk_size_exact_minimum(tmp_path):
    """chunk_size=4096 (exact sector size) should work without clamping."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(12288, seed=10)  # exactly 3 sectors
    src.write_bytes(payload)

    written = native.native_write(str(src), str(dst), 4096)

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_chunk_size_exact_maximum(tmp_path):
    """chunk_size=256 MiB (exact max) should not be clamped further."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(1_000_000, seed=11)
    src.write_bytes(payload)

    written = native.native_write(
        str(src), str(dst), 256 * 1024 * 1024
    )

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_chunk_size_over_max_clamped(tmp_path):
    """chunk_size > 256 MiB is clamped to 256 MiB silently."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(500_000, seed=12)
    src.write_bytes(payload)

    written = native.native_write(
        str(src), str(dst), 300 * 1024 * 1024
    )

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_chunk_size_below_minimum_clamped(tmp_path):
    """chunk_size < 4096 is clamped up to 4096."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(8192, seed=13)
    src.write_bytes(payload)

    written = native.native_write(str(src), str(dst), 1024)

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_progress_returns_non_none(tmp_path):
    """A callback that returns a value (not None) should not crash."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(8192, seed=14)
    src.write_bytes(payload)

    def callback(done, total):
        return "ignored"

    written = native.native_write(str(src), str(dst), 4096, callback)

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_progress_none_explicitly(tmp_path):
    """Passing progress=None explicitly should work identically to default."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(8192, seed=15)
    src.write_bytes(payload)

    written = native.native_write(str(src), str(dst), 4096, None)

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_large_payload_sector_aligned(tmp_path):
    """Write a payload that is an exact multiple of sector size."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(4096 * 100, seed=16)  # exactly 400 sectors
    src.write_bytes(payload)

    written = native.native_write(str(src), str(dst), 4096 * 10)

    assert written == len(payload)
    assert dst.read_bytes() == payload


@requires_native
def test_native_write_single_sector(tmp_path):
    """Write exactly one sector (4096 bytes)."""
    import core._native_writer as native

    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    payload = _blob(4096, seed=17)
    src.write_bytes(payload)

    written = native.native_write(str(src), str(dst), 4096)

    assert written == len(payload)
    assert dst.read_bytes() == payload


# ------------------------------------------------ load_native_writer edge cases


def test_load_native_writer_returns_none_on_import_error(monkeypatch):
    """_load_native_writer returns None when the extension is missing."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[union-attr]

    def block_native(name: str, *a: object, **kw: object) -> object:
        if name == "core._native_writer":
            raise ImportError("no native")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("importlib.import_module", block_native)
    assert writer._load_native_writer() is None


def test_load_native_writer_propagates_runtime_error(monkeypatch):
    """_load_native_writer propagates non-ImportError exceptions."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[union-attr]

    def boom(name: str, *a: object, **kw: object) -> object:
        if name == "core._native_writer":
            raise RuntimeError("corrupt extension")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("importlib.import_module", boom)
    with pytest.raises(RuntimeError, match="corrupt extension"):
        writer._load_native_writer()


# ---------------------------------------------------------- writer thread ----


class _FakeNative:
    def __init__(self):
        self.calls = []

    def native_write(self, path, device_path, chunk_size, progress=None):
        self.calls.append((path, device_path, chunk_size))
        return os.path.getsize(path)


def _monkeypatched_writer(monkeypatch, tmp_path, **kwargs):
    src = tmp_path / "iso.bin"
    payload = _blob(100_000, seed=2)
    src.write_bytes(payload)
    w = writer.UsbWriter(
        str(src), r"\\.\PHYSICALDRIVE9", chunk_size=4096, **kwargs
    )
    monkeypatch.setattr(w, "_open_drive", lambda: ctypes.c_void_p(12345))
    monkeypatch.setattr(w, "_drive_size", lambda handle: 10_000_000)
    monkeypatch.setattr(w, "_flush", lambda handle: None)
    return w, payload


def test_writer_inner_python_path_uses_chunk_size(tmp_path, monkeypatch):
    w, payload = _monkeypatched_writer(monkeypatch, tmp_path)
    sizes: list[int] = []
    monkeypatch.setattr(
        w, "_write_chunk", lambda handle, data: sizes.append(len(data))
    )

    w._run_inner()

    assert sizes and max(sizes) <= 4096
    assert sum(sizes) == len(payload)


def test_writer_inner_native_dispatch(tmp_path, monkeypatch):
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path, use_native=True)
    fake = _FakeNative()
    monkeypatch.setitem(sys.modules, "core._native_writer", fake)
    results = []
    w.finished.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert fake.calls == [
        (w.iso_path, w.drive_path, 4096)
    ]
    assert results == [(True, "")]
    assert w.chunk_size == 4096


def test_writer_inner_native_falls_back_when_missing(tmp_path, monkeypatch):
    """use_native=True without a built extension uses the Python loop."""
    monkeypatch.setitem(sys.modules, "core._native_writer", None)
    w, payload = _monkeypatched_writer(monkeypatch, tmp_path, use_native=True)
    sizes: list[int] = []
    monkeypatch.setattr(
        w, "_write_chunk", lambda handle, data: sizes.append(len(data))
    )

    w._run_inner()

    assert sizes and sum(sizes) == len(payload)


def test_writer_chunk_size_clamped_to_minimum():
    w = writer.UsbWriter("a.iso", r"\\.\PHYSICALDRIVE9", chunk_size=128)
    assert w.chunk_size == writer.DEFAULT_CHUNK_SIZE


# ------------------------------------------------- audit fixes (M1/M3/L3) ----


def test_writer_verify_cancel_reports_cancelled_not_success(tmp_path, monkeypatch):
    """M1 regression: cancelling during the post-write verification must
    emit finished(False, "cancelled") — never a false success, and no
    "verified" history entry may be produced."""
    w, _ = _monkeypatched_writer(
        monkeypatch, tmp_path, verify_after_write=True, verify_sha256=True
    )
    results: list[tuple[bool, str]] = []
    verify_results: list[tuple[bool, str]] = []
    w.finished.connect(lambda ok, msg: results.append((ok, msg)))
    w.verify_result.connect(lambda ok, msg, res: verify_results.append((ok, msg)))

    def fake_verify(device_path, source_iso=None, chunk_size=None,
                    retries=None, progress=None, is_cancelled=None,
                    scan_full_drive=False):
        # Simulate the user cancelling while the read-back is running.
        w._canceled = True
        return {
            "ok": False, "mismatches": [], "bad_sectors": [],
            "digest": "", "speed_mbps": 0.0, "error": "cancelled",
        }

    monkeypatch.setattr(writer.verify_mod, "verify_device", fake_verify)
    # Fake the drive handle away: chunk writes must not touch real hardware.
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: None)

    w.run()

    assert verify_results == [(False, "cancelled")]
    assert results == [(False, "cancelled")]


def test_writer_preflight_failure_closes_drive_handle(tmp_path, monkeypatch):
    """M3 regression: when the drive-size check fails before the write
    loop, the drive handle must be closed (previously it leaked and kept
    the drive busy until the app quit)."""
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path)
    results: list[tuple[bool, str]] = []
    closed: list = []
    w.finished.connect(lambda ok, msg: results.append((ok, msg)))
    real_close = writer.ctypes.windll.kernel32.CloseHandle

    def boom_size(handle):
        raise OSError("cannot query drive size")

    def recording_close(handle):
        closed.append(handle)
        return real_close(handle)

    monkeypatch.setattr(w, "_drive_size", boom_size)
    monkeypatch.setattr(writer.ctypes.windll.kernel32, "CloseHandle", recording_close)

    w.run()

    assert results == [(False, "cannot query drive size")]
    # Compare by value: ctypes pointer equality is identity in newer pythons.
    assert [h.value for h in closed] == [12345]


def test_filecopy_cancel_before_start_reports_cancelled(tmp_path, monkeypatch):
    """L3: cancel before the file-copy path begins must not proceed."""
    monkeypatch.setattr(
        writer.diskpart, "resolve_write_mode", lambda mode, iso: "filecopy"
    )
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path)
    results: list[tuple[bool, str]] = []
    w.finished.connect(lambda ok, msg: results.append((ok, msg)))
    w.cancel()

    w.run()

    assert results == [(False, "cancelled")]


def test_filecopy_cancel_midway_reports_cancelled(tmp_path, monkeypatch):
    """L3: cancelling between the prepare/copy phases must abort cleanly
    instead of continuing to flash or hanging."""
    monkeypatch.setattr(
        writer.diskpart, "resolve_write_mode", lambda mode, iso: "filecopy"
    )
    monkeypatch.setattr(
        writer.diskpart, "prepare_partition",
        lambda number, scheme, filesystem: "X",
    )

    def cancel_after_partition(iso_path, letter):
        w._canceled = True

    monkeypatch.setattr(
        writer.diskpart, "copy_iso_files", cancel_after_partition
    )
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path)
    results: list[tuple[bool, str]] = []
    w.finished.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [(False, "cancelled")]


# ----------------------------------------------- volume-lock regressions ----


class _FailingVolumeKernel:
    """Minimal kernel32 fake whose volume open always fails."""

    def __init__(self, invalid_handle: int) -> None:
        self.invalid_handle = invalid_handle
        self.execution_states: list[int] = []
        self.opened_paths: list[str] = []

    def SetThreadExecutionState(self, state: int) -> int:
        self.execution_states.append(state)
        return state

    def CreateFileW(
        self,
        path,
        access,
        share_mode,
        security,
        creation,
        flags,
        template,
    ) -> int:
        self.opened_paths.append(path)
        return self.invalid_handle


def test_writer_volume_open_failure_aborts(tmp_path, monkeypatch, capsys):
    """A volume that cannot be opened for locking must abort the write."""
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path)
    w.letters = ["X"]
    kernel = _FailingVolumeKernel(w._INVALID_HANDLE_VALUE)
    results: list[tuple[bool, str]] = []
    inner_calls: list[bool] = []
    w.finished.connect(lambda ok, msg: results.append((ok, msg)))

    monkeypatch.setattr(
        writer.diskpart, "resolve_write_mode", lambda mode, iso: "raw"
    )
    monkeypatch.setattr(writer, "_kernel32", lambda: kernel)
    monkeypatch.setattr(w, "_run_inner", lambda: inner_calls.append(True))

    w.run()

    captured = capsys.readouterr()
    assert kernel.opened_paths == [r"\\.\X:"]
    assert inner_calls == []
    assert results == [
        (False, "Volume X: could not be opened for locking.")
    ]
    assert "success" not in captured.out.lower()


def test_wipe_volume_open_failure_aborts(monkeypatch, capsys):
    """WipeWorker must abort when a target volume cannot be locked."""
    worker = WipeWorker(
        r"\\.\PHYSICALDRIVE9",
        letters=["X"],
        verify=False,
    )
    kernel = _FailingVolumeKernel(worker._INVALID_HANDLE_VALUE)
    results: list[tuple[bool, str]] = []
    inner_calls: list[bool] = []
    worker.finished.connect(lambda ok, msg: results.append((ok, msg)))

    monkeypatch.setattr(worker, "_kernel32", lambda: kernel)
    monkeypatch.setattr(worker, "_run_inner", lambda: inner_calls.append(True))

    worker.run()

    captured = capsys.readouterr()
    assert kernel.opened_paths == [r"\\.\X:"]
    assert inner_calls == []
    assert results == [
        (False, "Volume X: could not be opened for locking.")
    ]
    assert "success" not in captured.out.lower()


# -------------------------------------------- file-copy verification fix ----


def test_filecopy_mode_runs_verification_after_copy(tmp_path, monkeypatch):
    """A successful file-copy write must run requested verification."""
    w, _ = _monkeypatched_writer(
        monkeypatch,
        tmp_path,
        verify_after_write=True,
        verify_sha256=True,
    )
    calls: list[str] = []
    results: list[tuple[bool, str]] = []
    w.finished.connect(lambda ok, msg: results.append((ok, msg)))

    monkeypatch.setattr(
        writer.diskpart, "resolve_write_mode", lambda mode, iso: "filecopy"
    )
    monkeypatch.setattr(w, "_run_filecopy", lambda: calls.append("filecopy"))
    monkeypatch.setattr(
        w, "_verify_after_write", lambda: calls.append("verify")
    )

    w.run()

    assert calls == ["filecopy", "verify"]
    assert results == [(True, "")]


# ------------------------------------------------------------ benchmark -----


@requires_native
def test_microbenchmark_python_vs_native(tmp_path):
    """Compare both write paths on the same payload (timing only reported)."""
    src = tmp_path / "bench_src.bin"
    payload = _blob(32 * 1024 * 1024, seed=42)
    src.write_bytes(payload)

    python_dst = tmp_path / "bench_py.bin"
    start = time.perf_counter()
    written = writer.write_stream(
        str(src), str(python_dst), chunk_size=8 * 1024 * 1024
    )
    python_elapsed = time.perf_counter() - start
    assert written == len(payload)
    assert python_dst.read_bytes() == payload

    native_dst = tmp_path / "bench_native.bin"
    start = time.perf_counter()
    written = writer.write_stream(
        str(src), str(native_dst), chunk_size=8 * 1024 * 1024, use_native=True
    )
    native_elapsed = time.perf_counter() - start
    assert written == len(payload)
    assert native_dst.read_bytes() == payload

    logger.info(
        "benchmark 32 MiB: python %.2fs (%.1f MB/s), native %.2fs (%.1f MB/s)",
        python_elapsed,
        len(payload) / python_elapsed / 1_000_000,
        native_elapsed,
        len(payload) / native_elapsed / 1_000_000,
    )
