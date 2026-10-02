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
from pathlib import Path

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
    # Final chunk is padded to sector alignment, so sum >= payload.
    import math
    assert sum(sizes) >= len(payload)
    assert sum(sizes) == math.ceil(len(payload) / 4096) * 4096


def test_writer_inner_native_dispatch(tmp_path, monkeypatch):
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path, use_native=True)
    fake = _FakeNative()
    monkeypatch.setitem(sys.modules, "core._native_writer", fake)
    results = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

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

    import math
    assert sizes and sum(sizes) == math.ceil(len(payload) / 4096) * 4096


def test_writer_chunk_size_clamped_to_minimum():
    w = writer.UsbWriter("a.iso", r"\\.\PHYSICALDRIVE9", chunk_size=128)
    assert w.chunk_size == writer.DEFAULT_CHUNK_SIZE


# ------------------------------------------------------- short stream (B35) --


class _ShortNative:
    """Extension that reports fewer bytes than it actually copied."""

    def native_write(self, path, device_path, chunk_size, progress=None):
        return 1


def test_write_stream_native_short_stream_raises(monkeypatch, tmp_path):
    """A short stream is an error, never a successful write."""
    src = tmp_path / "iso.bin"
    src.write_bytes(_blob(4096 * 3, seed=5))
    monkeypatch.setitem(sys.modules, "core._native_writer", _ShortNative())

    with pytest.raises(OSError, match="stopped short"):
        writer.write_stream(
            str(src), str(tmp_path / "dst.bin"), 4096, use_native=True
        )


def test_writer_inner_native_short_stream_reports_failure(
    tmp_path, monkeypatch
):
    """_run_native must not report 100% for a truncated image."""
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path, use_native=True)
    monkeypatch.setitem(sys.modules, "core._native_writer", _ShortNative())
    results = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert len(results) == 1
    ok, msg = results[0]
    assert ok is False
    assert msg.startswith("native write stopped short")


def test_write_stream_native_full_stream_returns_count(monkeypatch, tmp_path):
    """The guard fires on short streams only - an exact copy still succeeds."""
    src = tmp_path / "iso.bin"
    payload = _blob(4096 * 3, seed=7)
    src.write_bytes(payload)
    dst = tmp_path / "dst.bin"

    class _Exact:
        def native_write(self, path, device_path, chunk_size, progress=None):
            return os.path.getsize(path)

    monkeypatch.setitem(sys.modules, "core._native_writer", _Exact())
    written = writer.write_stream(
        str(src), str(dst), 4096, use_native=True
    )
    assert written == len(payload)


# ------------------------------------------------- audit fixes (M1/M3/L3) ----


def test_writer_verify_cancel_reports_cancelled_not_success(tmp_path, monkeypatch):
    """M1 regression: cancelling during the post-write verification must
    emit done(False, "cancelled") — never a false success, and no
    "verified" history entry may be produced."""
    w, _ = _monkeypatched_writer(
        monkeypatch, tmp_path, verify_after_write=True, verify_sha256=True
    )
    results: list[tuple[bool, str]] = []
    verify_results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))
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
    w.done.connect(lambda ok, msg: results.append((ok, msg)))
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
    w.done.connect(lambda ok, msg: results.append((ok, msg)))
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
        lambda number, scheme, filesystem, target="auto": "X",
    )

    def cancel_after_partition(iso_path, letter, **_kw):
        w._canceled = True

    monkeypatch.setattr(
        writer.diskpart, "copy_iso_files", cancel_after_partition
    )
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path)
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

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
    from core.deviceio import _INVALID_HANDLE_VALUE
    kernel = _FailingVolumeKernel(_INVALID_HANDLE_VALUE)
    results: list[tuple[bool, str]] = []
    inner_calls: list[bool] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    monkeypatch.setattr(
        writer.diskpart, "resolve_write_mode", lambda mode, iso: "raw"
    )
    monkeypatch.setattr("core.deviceio.kernel32", lambda: kernel)
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
    from core.deviceio import _INVALID_HANDLE_VALUE
    worker = WipeWorker(
        r"\\.\PHYSICALDRIVE9",
        letters=["X"],
        verify=False,
    )
    kernel = _FailingVolumeKernel(_INVALID_HANDLE_VALUE)
    results: list[tuple[bool, str]] = []
    inner_calls: list[bool] = []
    worker.done.connect(lambda ok, msg: results.append((ok, msg)))

    monkeypatch.setattr("core.deviceio.kernel32", lambda: kernel)
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
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    monkeypatch.setattr(
        writer.diskpart, "resolve_write_mode", lambda mode, iso: "filecopy"
    )
    monkeypatch.setattr(w, "_run_filecopy", lambda: calls.append("filecopy"))
    monkeypatch.setattr(
        w, "_verify_after_write", lambda **kw: calls.append("verify")
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


# ---------------------------------------- fleet resume regression (v2.0.x) ----


def _resume_manifest(w, payload, **overrides):
    """Manifest matching what the writer builds, with an override field."""
    from core import jobs

    fields = {
        "source_path": w.iso_path,
        "source_size": len(payload),
        "source_sha256": jobs.source_sha256(w.iso_path),
        "target_fingerprint": w.target_fingerprint,
        "target_size": 9_999_999_999,  # GB-rounded detection value
        "options": {
            "chunk_size": w.chunk_size,
            "verify_after_write": w.verify_after_write,
            "verify_sha256": w.verify_sha256,
            "bad_block_scan": w.bad_block_scan,
        },
        "state": "writing",
    }
    fields.update(overrides)
    return jobs.JobManifest(**fields)


def test_writer_resume_adopts_exact_size_for_fresh_manifest(tmp_path, monkeypatch):
    """A manifest recorded from a GB-rounded detection capacity must resume:
    at checkpoint 0 the exact IOCTL capacity replaces the recorded value
    before identity validation runs (fleet flash regression)."""
    from core import jobs

    w, payload = _monkeypatched_writer(monkeypatch, tmp_path, resume=True)
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: None)
    jobs.save_manifest(w.manifest_path, _resume_manifest(w, payload))

    w._run_inner()  # must not raise "cannot resume: target size changed"

    assert w._finished is False
    # A completed write persists the adopted size, then removes the manifest.
    assert not os.path.exists(w.manifest_path)


def test_writer_resume_rejects_size_change_after_checkpoint(tmp_path, monkeypatch):
    """Once bytes are on disk the recorded capacity is authoritative: a
    mismatch must fail the job and persist a resumable error."""
    from core import jobs

    w, payload = _monkeypatched_writer(monkeypatch, tmp_path, resume=True)
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: None)
    jobs.save_manifest(
        w.manifest_path,
        _resume_manifest(w, payload, checkpoint_bytes=len(payload) // 2),
    )
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w._run_inner()

    assert results and results[0][0] is False
    assert "target size changed" in results[0][1]
    reloaded = jobs.load_manifest(w.manifest_path)
    assert reloaded.state == "resumable"
    assert "target size changed" in (reloaded.error or "")


def test_writer_resume_corrupt_manifest_fails_cleanly(tmp_path, monkeypatch):
    """A manifest that cannot be parsed is a ValueError, not an OSError;
    it must still finish the job instead of escaping the thread."""
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path, resume=True)
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: None)
    Path(w.manifest_path).write_text("{not json", encoding="utf-8")
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w._run_inner()

    assert results and results[0][0] is False
    assert results[0][1]  # error message surfaced, not an empty failure


# --------------------------------- write-pipeline findings (agent B) ------


def test_open_drive_is_exclusive_only_for_letterless_drives(tmp_path, monkeypatch):
    """B03: no letters ⇒ no FSCTL volume locks exist, so the open handle
    itself must deny sharing (exclusive=True); with letters the handle stays
    shareable and the volume locks do the locking."""
    calls: list[dict] = []

    def fake_open(path, *, write, flags=0, exclusive=False):
        calls.append({"path": path, "write": write, "exclusive": exclusive})
        return 4242

    monkeypatch.setattr(writer, "open_drive", fake_open)

    letterless = writer.UsbWriter(str(tmp_path / "a.iso"), r"\\.\PHYSICALDRIVE9")
    assert letterless.letters == []
    assert letterless._open_drive() == 4242

    lettered = writer.UsbWriter(
        str(tmp_path / "a.iso"), r"\\.\PHYSICALDRIVE9", letters=["E"]
    )
    assert lettered._open_drive() == 4242

    assert [c["exclusive"] for c in calls] == [True, False]
    assert all(c["write"] for c in calls)
    assert all(c["path"] == r"\\.\PHYSICALDRIVE9" for c in calls)


def test_open_drive_sharing_violation_message_is_helpful(tmp_path, monkeypatch):
    """B03: an exclusive open that loses the race must still tell the user
    what to do instead of surfacing a bare "(error 32)"."""

    def busy_open(path, *, write, flags=0, exclusive=False):
        raise OSError(f"could not open {path} for write (error 32)")

    monkeypatch.setattr(writer, "open_drive", busy_open)
    w = writer.UsbWriter(str(tmp_path / "a.iso"), r"\\.\PHYSICALDRIVE9")

    with pytest.raises(OSError) as excinfo:
        w._open_drive()

    message = str(excinfo.value)
    assert "in use" in message
    assert "sharing violation" in message
    assert "try again" in message
    assert isinstance(excinfo.value.__cause__, OSError)  # original preserved


def test_writer_cancel_inside_write_retry_reports_cancelled(tmp_path, monkeypatch):
    """B06: deviceio._Cancelled is neither OSError nor ValueError; it must
    surface as done(False, "cancelled") with a resumable manifest — not
    run()'s empty-message failure that leaves state="writing"."""
    from core import jobs
    from core.deviceio import _Cancelled

    w, payload = _monkeypatched_writer(monkeypatch, tmp_path, resume=True)
    jobs.save_manifest(w.manifest_path, _resume_manifest(w, payload))

    def cancelling_write(handle, data):
        raise _Cancelled()

    monkeypatch.setattr(w, "_write_chunk", cancelling_write)
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [(False, "cancelled")]  # exactly one emission
    reloaded = jobs.load_manifest(w.manifest_path)
    assert reloaded.state == "resumable"
    assert reloaded.error == "write cancelled"


def test_wipe_cancel_inside_write_retry_reports_cancelled(monkeypatch):
    """B06: WipeWorker's except OSError had the same _Cancelled escape."""
    from core.deviceio import _Cancelled

    worker = WipeWorker(r"\\.\PHYSICALDRIVE9", verify=False)
    monkeypatch.setattr(worker, "_open_drive", lambda: 1234)
    monkeypatch.setattr(
        worker, "_drive_size", lambda handle: 4 * 1024 * 1024
    )
    monkeypatch.setattr(worker, "_lock_volumes", list)
    monkeypatch.setattr(worker, "_unlock_volumes", lambda held: None)
    closed: list[int] = []

    def record_close(handle):
        closed.append(int(getattr(handle, "value", handle)))
        return 1

    monkeypatch.setattr(ctypes.windll.kernel32, "CloseHandle", record_close)

    def cancelling_write(handle, data):
        raise _Cancelled()

    monkeypatch.setattr(worker, "_write_chunk", cancelling_write)
    results: list[tuple[bool, str]] = []
    worker.done.connect(lambda ok, msg: results.append((ok, msg)))

    worker.run()

    assert results == [(False, "cancelled")]  # not done(False, "")
    assert 1234 in closed  # drive handle still released


def test_native_dispatch_closes_preflight_handle_before_native(tmp_path, monkeypatch):
    """L01: the Python pre-flight handle must be closed BEFORE the native
    extension reopens the device with dwShareMode = 0, otherwise the native
    open hits ERROR_SHARING_VIOLATION (32)."""
    w, _ = _monkeypatched_writer(monkeypatch, tmp_path, use_native=True)
    events: list[str] = []
    real_close = writer.ctypes.windll.kernel32.CloseHandle

    def recording_close(handle):
        events.append("close-handle")
        return real_close(handle)

    monkeypatch.setattr(
        writer.ctypes.windll.kernel32, "CloseHandle", recording_close
    )

    class _OrderingNative:
        def native_write(self, path, device_path, chunk_size, progress=None):
            events.append("native-write")
            return os.path.getsize(path)

    monkeypatch.setitem(sys.modules, "core._native_writer", _OrderingNative())
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert events == ["close-handle", "native-write"]  # close strictly first
    assert results == [(True, "")]


def test_resume_seeks_source_and_drive_to_same_offset(tmp_path, monkeypatch):
    """L02: an unaligned checkpoint must move drive AND source to the same
    aligned offset, otherwise the remainder of the image is written shifted."""
    from core import jobs

    payload = _blob(32 * 1024, seed=5)
    src = tmp_path / "iso.bin"
    src.write_bytes(payload)
    w = writer.UsbWriter(
        str(src), r"\\.\PHYSICALDRIVE9", chunk_size=4096, resume=True
    )
    monkeypatch.setattr(w, "_open_drive", lambda: ctypes.c_void_p(12345))
    monkeypatch.setattr(w, "_drive_size", lambda handle: 10_000_000)
    monkeypatch.setattr(w, "_flush", lambda handle: None)
    seek_calls: list[int] = []
    monkeypatch.setattr(
        w, "_seek_drive", lambda handle, offset: seek_calls.append(offset)
    )
    chunks: list[bytes] = []
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: chunks.append(data))
    saved = 5000  # unaligned: 5000 - 5000 % 4096 == 4096
    jobs.save_manifest(
        w.manifest_path,
        _resume_manifest(
            w, payload, checkpoint_bytes=saved, target_size=10_000_000
        ),
    )

    w._run_inner()

    assert seek_calls == [4096]
    assert chunks[0] == payload[4096 : 4096 + 4096]  # source read from 4096 too
    assert w._finished is False


def test_resume_below_one_sector_falls_back_to_zero(tmp_path, monkeypatch):
    """R2: a checkpoint at 0 < saved < 4096 must align DOWN to 0.

    The drive handle is opened FILE_FLAG_NO_BUFFERING, so it must never be
    left at an unaligned offset - SetFilePointerEx accepts the seek, the
    next ReadFile/WriteFile does not. The old `aligned <= 0 -> saved`
    fallback re-introduced exactly that for sub-sector checkpoints.
    """
    from core import jobs

    payload = _blob(32 * 1024, seed=7)
    src = tmp_path / "iso.bin"
    src.write_bytes(payload)
    w = writer.UsbWriter(
        str(src), r"\\.\PHYSICALDRIVE9", chunk_size=4096, resume=True
    )
    monkeypatch.setattr(w, "_open_drive", lambda: ctypes.c_void_p(12345))
    monkeypatch.setattr(w, "_drive_size", lambda handle: 10_000_000)
    monkeypatch.setattr(w, "_flush", lambda handle: None)
    seek_calls: list[int] = []
    monkeypatch.setattr(
        w, "_seek_drive", lambda handle, offset: seek_calls.append(offset)
    )
    chunks: list[bytes] = []
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: chunks.append(data))
    jobs.save_manifest(
        w.manifest_path,
        _resume_manifest(
            w, payload, checkpoint_bytes=1000, target_size=10_000_000
        ),
    )

    w._run_inner()

    assert seek_calls == [0]  # sector-aligned, never 1000
    assert chunks[0] == payload[0:4096]
    assert w._finished is False


def test_checkpoint_saves_once_per_interval(tmp_path, monkeypatch):
    """L03: checkpoints are gated by an explicit next_checkpoint threshold —
    one durable save per 10 MiB of source bytes, independent of chunk size
    (the old `source_written % 10MiB < chunk_size` modulo fired whenever the
    residue landed inside one chunk: ~80% of chunks at the 8 MiB default)."""
    from core import jobs

    size = 40 * 1024 * 1024
    src = tmp_path / "iso.bin"
    src.write_bytes(bytes(size))
    w = writer.UsbWriter(
        str(src), r"\\.\PHYSICALDRIVE9", chunk_size=64 * 1024, resume=True
    )
    monkeypatch.setattr(w, "_open_drive", lambda: ctypes.c_void_p(12345))
    monkeypatch.setattr(w, "_drive_size", lambda handle: 10_000_000_000)
    monkeypatch.setattr(w, "_flush", lambda handle: None)
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: None)
    jobs.save_manifest(
        w.manifest_path,
        _resume_manifest(w, bytes(size), target_size=10_000_000_000),
    )
    saves: list[tuple[int, str]] = []
    monkeypatch.setattr(
        jobs,
        "save_manifest",
        lambda path, manifest: saves.append(
            (manifest.checkpoint_bytes, manifest.state)
        ),
    )

    w._run_inner()

    interval = writer.CHECKPOINT_INTERVAL
    assert saves == [
        (interval, "writing"),
        (interval * 2, "writing"),
        (interval * 3, "writing"),
        (interval * 4, "writing"),
        (size, "written"),  # final save on completion
    ]
    # ~size/10MiB durable saves, nowhere near the 640-chunk total.
    assert len(saves) - 1 == size // interval
    assert size // w.chunk_size == 640


def test_checkpoint_follows_explicit_threshold_for_uneven_chunks(tmp_path, monkeypatch):
    """L03: saves track the explicit next_checkpoint (start 10 MiB, +10 MiB
    per save) even when the chunk size does not divide the interval: each
    save is the first chunk at/a.ter the current threshold — 10→12, 20→21,
    30→30, 40→42 MiB for a 3 MiB chunk — and the total is one save per
    10 MiB, not one per chunk."""
    from core import jobs

    mib = 1024 * 1024
    size = 48 * mib
    chunk = 3 * mib
    src = tmp_path / "iso.bin"
    src.write_bytes(bytes(size))
    w = writer.UsbWriter(
        str(src), r"\\.\PHYSICALDRIVE9", chunk_size=chunk, resume=True
    )
    assert w.chunk_size == chunk
    monkeypatch.setattr(w, "_open_drive", lambda: ctypes.c_void_p(12345))
    monkeypatch.setattr(w, "_drive_size", lambda handle: 10_000_000_000)
    monkeypatch.setattr(w, "_flush", lambda handle: None)
    monkeypatch.setattr(w, "_write_chunk", lambda handle, data: None)
    jobs.save_manifest(
        w.manifest_path,
        _resume_manifest(w, bytes(size), target_size=10_000_000_000),
    )
    saves: list[tuple[int, str]] = []
    monkeypatch.setattr(
        jobs,
        "save_manifest",
        lambda path, manifest: saves.append(
            (manifest.checkpoint_bytes, manifest.state)
        ),
    )

    w._run_inner()

    assert [cp for cp, state in saves if state == "writing"] == [
        12 * mib,
        21 * mib,
        30 * mib,
        42 * mib,
    ]
    assert saves[-1] == (size, "written")
    assert len([1 for cp, state in saves if state == "writing"]) == (
        size // writer.CHECKPOINT_INTERVAL
    )
    assert 16 == size // chunk  # 16 chunks, only 4 mid-write saves


def _filecopy_setup(monkeypatch, tmp_path, **kwargs):
    """UsbWriter whose write mode resolves to filecopy with stubbed tools."""
    monkeypatch.setattr(
        writer.diskpart, "resolve_write_mode", lambda mode, iso: "filecopy"
    )
    monkeypatch.setattr(
        writer.diskpart,
        "prepare_partition",
        lambda n, s, f, t="auto": "X",
    )
    monkeypatch.setattr(
        writer.diskpart, "copy_iso_files", lambda iso, letter, **_kw: None
    )
    monkeypatch.setattr(
        writer.iso_mod, "list_iso_paths", lambda path: {"casper/vmlinuz"}
    )
    return _monkeypatched_writer(monkeypatch, tmp_path, **kwargs)


def test_filecopy_success_removes_stale_manifest(tmp_path, monkeypatch):
    """L10: a fleet manifest orphaned by a filecopy-resolving write must be
    deleted on success, mirroring the raw path."""
    from core import jobs

    w, payload = _filecopy_setup(monkeypatch, tmp_path)
    jobs.save_manifest(w.manifest_path, _resume_manifest(w, payload))
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [(True, "")]
    assert not os.path.exists(w.manifest_path)


def test_filecopy_cancel_persists_resumable_manifest(tmp_path, monkeypatch):
    """L10: cancelling a filecopy write must not leave the manifest stuck in
    state="writing"."""
    from core import jobs

    w, payload = _filecopy_setup(monkeypatch, tmp_path)

    def cancel_during_copy(iso, letter, **_kw):
        w.cancel()

    monkeypatch.setattr(writer.diskpart, "copy_iso_files", cancel_during_copy)
    jobs.save_manifest(w.manifest_path, _resume_manifest(w, payload))
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [(False, "cancelled")]
    reloaded = jobs.load_manifest(w.manifest_path)
    assert reloaded.state == "resumable"
    assert reloaded.error == "write cancelled"


def test_filecopy_cancel_raised_mid_copy_reports_cancelled(
    tmp_path, monkeypatch
):
    """L22: _Cancelled from a killed robocopy/dism child must report
    "cancelled" (not a tool failure) and persist a resumable manifest."""
    from core import jobs
    from core.deviceio import _Cancelled

    w, payload = _filecopy_setup(monkeypatch, tmp_path)
    seen: dict[str, object] = {}

    def kill_copy(iso, letter, **kwargs):
        seen.update(kwargs)
        w.cancel()
        raise _Cancelled()

    monkeypatch.setattr(writer.diskpart, "copy_iso_files", kill_copy)
    jobs.save_manifest(w.manifest_path, _resume_manifest(w, payload))
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [(False, "cancelled")]
    assert callable(seen.get("is_cancelled"))
    assert seen["is_cancelled"]() is True  # cancel tripped during the copy
    reloaded = jobs.load_manifest(w.manifest_path)
    assert reloaded.state == "resumable"
    assert reloaded.error == "write cancelled"


def test_filecopy_tool_failure_persists_resumable_manifest(tmp_path, monkeypatch):
    """L10: an OSError from diskpart/robocopy/dism must record the error on
    the manifest before run() reports the failure."""
    from core import jobs

    w, payload = _filecopy_setup(monkeypatch, tmp_path)

    def failing_copy(iso, letter, **_kw):
        raise OSError("robocopy failed copying files onto the drive")

    monkeypatch.setattr(writer.diskpart, "copy_iso_files", failing_copy)
    jobs.save_manifest(w.manifest_path, _resume_manifest(w, payload))
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [
        (False, "robocopy failed copying files onto the drive")
    ]
    reloaded = jobs.load_manifest(w.manifest_path)
    assert reloaded.state == "resumable"
    assert "robocopy" in (reloaded.error or "")


def test_filecopy_persistence_failure_fails_flash(tmp_path, monkeypatch):
    """L12: create_persistence reporting ok=False must fail the job — never a
    success with an unformatted casper-rw."""
    from core import jobs

    w, payload = _filecopy_setup(
        monkeypatch, tmp_path, persistence=True, persistence_size_mb=512
    )
    jobs.save_manifest(w.manifest_path, _resume_manifest(w, payload))
    monkeypatch.setattr(
        writer.persistence,
        "create_persistence",
        lambda root, mb, paths: (
            False,
            "casper-rw is NOT formatted: no mke2fs/makefs or WSL found",
        ),
    )
    notes: list[str] = []
    results: list[tuple[bool, str]] = []
    w.note.connect(notes.append)
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [
        (
            False,
            (
                "persistence setup failed: casper-rw is NOT formatted: "
                "no mke2fs/makefs or WSL found"
            ),
        )
    ]
    assert len(results) == 1  # no trailing success emission
    assert notes  # the detail is still surfaced as a note
    reloaded = jobs.load_manifest(w.manifest_path)
    assert reloaded.state == "resumable"
    assert "persistence setup failed" in (reloaded.error or "")


def test_filecopy_cancel_after_persistence_skips_tpm_patch(tmp_path, monkeypatch):
    """L22: cancel is checked between every tool phase — a cancel raised by
    the persistence step must not start the TPM patch."""
    w, _ = _filecopy_setup(
        monkeypatch, tmp_path, persistence=True, bypass_tpm=True
    )

    def cancel_during_persistence(root, mb, paths):
        w.cancel()
        return True, "persistence created"

    monkeypatch.setattr(
        writer.persistence, "create_persistence", cancel_during_persistence
    )
    tpm_calls: list[str] = []
    monkeypatch.setattr(
        "core.tpm_bypass.patch_boot_wim_on_usb",
        lambda letter: tpm_calls.append(letter),
    )
    results: list[tuple[bool, str]] = []
    w.done.connect(lambda ok, msg: results.append((ok, msg)))

    w.run()

    assert results == [(False, "cancelled")]
    assert tpm_calls == []

