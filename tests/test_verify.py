"""Verification tests: streaming SHA-256, byte-compare mismatch detection
with offsets, bad-sector detection with read retries, cancellation, and the
post-write verification UI options.

All device tests use plain temporary files (CreateFile/ReadFile work on
files too), so no real hardware is needed.
"""

import hashlib

import pytest

from core import verify


def _blob(size: int, seed: int = 0) -> bytes:
    data = bytearray(size)
    for i in range(0, size, 64):
        data[i : i + 64] = bytes((seed + i // 64) % 256 for _ in range(64))
    return bytes(data)


def _write_pair(tmp_path, size, seed=0):
    """Create (iso_path, device_path) where the device matches the ISO."""
    payload = _blob(size, seed)
    iso = tmp_path / "iso.bin"
    iso.write_bytes(payload)
    device = tmp_path / "device.bin"
    device.write_bytes(payload)
    return iso, device, payload


# ------------------------------------------------------------- sha-256 ------


def test_compute_sha256_matches_hashlib(tmp_path):
    iso, _, payload = _write_pair(tmp_path, 1_000_000, seed=1)
    ok, digest = verify.compute_sha256(str(iso), chunk_size=64 * 1024)
    assert ok
    assert digest == hashlib.sha256(payload).hexdigest()


def test_compute_sha256_missing_file():
    ok, message = verify.compute_sha256(r"C:\flint_no_such_file.bin")
    assert not ok
    assert "could not open" in message


def test_compute_sha256_progress(tmp_path):
    iso, _, payload = _write_pair(tmp_path, 300_000, seed=2)
    calls: list[tuple[int, int]] = []
    ok, _ = verify.compute_sha256(
        str(iso),
        chunk_size=64 * 1024,
        progress=lambda d, t: calls.append((d, t)),
    )
    assert ok
    assert calls[-1] == (len(payload), len(payload))
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)


# ------------------------------------------------------- verify_device ------


def test_verify_identical_device(tmp_path):
    iso, device, payload = _write_pair(tmp_path, 2_000_000, seed=3)
    result = verify.verify_device(
        str(device), source_iso=str(iso), chunk_size=64 * 1024
    )
    assert result["ok"] is True
    assert result["mismatches"] == []
    assert result["bad_sectors"] == []
    assert result["digest"] == hashlib.sha256(payload).hexdigest()
    assert result["speed_mbps"] > 0
    assert result["error"] == ""


def test_verify_detects_single_corruption_at_known_offset(tmp_path):
    iso, device, payload = _write_pair(tmp_path, 1_000_000, seed=4)
    corrupted = bytearray(payload)
    corrupted[500_000] ^= 0xFF
    device.write_bytes(corrupted)

    result = verify.verify_device(
        str(device), source_iso=str(iso), chunk_size=128 * 1024
    )

    assert result["ok"] is False
    assert len(result["mismatches"]) == 1
    offset, length, expected, actual = result["mismatches"][0]
    assert offset <= 500_000 < offset + length
    assert expected != actual
    assert expected == payload[offset : offset + length]
    assert actual == bytes(corrupted)[offset : offset + length]


def test_verify_detects_multiple_corruptions(tmp_path):
    iso, device, payload = _write_pair(tmp_path, 1_000_000, seed=5)
    corrupted = bytearray(payload)
    for pos in (10_000, 400_000, 900_000):
        corrupted[pos] ^= 0x55
    device.write_bytes(corrupted)

    result = verify.verify_device(
        str(device), source_iso=str(iso), chunk_size=64 * 1024
    )

    assert result["ok"] is False
    assert len(result["mismatches"]) == 3
    offsets = [m[0] for m in result["mismatches"]]
    assert offsets == sorted(set(offsets))
    for offset, length, expected, actual in result["mismatches"]:
        assert expected != actual
        assert expected == payload[offset : offset + length]


def test_verify_device_smaller_than_iso(tmp_path):
    iso, _, _ = _write_pair(tmp_path, 500_000, seed=6)
    device = tmp_path / "small.bin"
    device.write_bytes(_blob(100_000, seed=7))

    result = verify.verify_device(str(device), source_iso=str(iso))

    assert result["ok"] is False
    assert "smaller" in result["error"]


def test_verify_expected_digest(tmp_path):
    _, device, payload = _write_pair(tmp_path, 200_000, seed=8)
    good = hashlib.sha256(payload).hexdigest()
    result = verify.verify_device(
        str(device), expected_sha256=good, chunk_size=64 * 1024
    )
    assert result["ok"] is True
    wrong = hashlib.sha256(b"nope").hexdigest()
    result = verify.verify_device(
        str(device), expected_sha256=wrong, chunk_size=64 * 1024
    )
    assert result["ok"] is False
    assert result["mismatches"] == []


def test_verify_progress_reports_total(tmp_path):
    iso, device, payload = _write_pair(tmp_path, 250_000, seed=9)
    calls: list[tuple[int, int]] = []
    verify.verify_device(
        str(device),
        source_iso=str(iso),
        chunk_size=64 * 1024,
        progress=lambda d, t: calls.append((d, t)),
    )
    assert calls[-1] == (len(payload), len(payload))
    assert all(t == len(payload) for _, t in calls)


# ------------------------------------------------- bad sectors & retries ----

def _patch_readfile(monkeypatch, fake):
    """Replace kernel32.ReadFile (retry loop runs inside _read_chunk)."""
    kernel32 = verify.ctypes.windll.kernel32
    real_read = kernel32.ReadFile

    def flaky_read(handle, buffer, count, byref_read, overlapped):
        if fake(byref_read):
            return 0  # simulated failure
        return real_read(handle, buffer, count, byref_read, overlapped)

    monkeypatch.setattr(kernel32, "ReadFile", flaky_read)
    return real_read


def test_verify_recovers_after_retries(tmp_path, monkeypatch):
    iso, device, payload = _write_pair(tmp_path, 300_000, seed=10)
    calls = {"n": 0}

    def fail_first_two(byref_read):
        calls["n"] += 1
        return calls["n"] <= 2

    _patch_readfile(monkeypatch, fail_first_two)
    result = verify.verify_device(
        str(device),
        source_iso=str(iso),
        chunk_size=64 * 1024,
        retries=3,
    )

    assert result["ok"] is True
    assert result["bad_sectors"] == []
    assert result["digest"] == hashlib.sha256(payload).hexdigest()
    # two failed attempts + one successful retry on the first chunk, then
    # one call per remaining chunk
    chunks = (len(payload) + 64 * 1024 - 1) // (64 * 1024)
    assert calls["n"] == 2 + chunks


def test_verify_reports_persistent_bad_sector(tmp_path, monkeypatch):
    iso, device, _ = _write_pair(tmp_path, 300_000, seed=11)
    calls = {"n": 0}

    def fail_second_chunk(byref_read):
        """Chunk 2 (calls 2-5 with retries=3) can never be read."""
        calls["n"] += 1
        return 2 <= calls["n"] <= 5

    _patch_readfile(monkeypatch, fail_second_chunk)
    result = verify.verify_device(
        str(device),
        source_iso=str(iso),
        chunk_size=64 * 1024,
        retries=3,
    )

    assert result["ok"] is False
    assert result["bad_sectors"] == [64 * 1024]
    assert result["mismatches"] == []


def test_verify_zero_retries_means_single_attempt(tmp_path, monkeypatch):
    iso, device, _ = _write_pair(tmp_path, 100_000, seed=12)
    calls = {"n": 0}

    def always_fail(byref_read):
        calls["n"] += 1
        return True

    _patch_readfile(monkeypatch, always_fail)
    result = verify.verify_device(
        str(device),
        source_iso=str(iso),
        chunk_size=64 * 1024,
        retries=0,
    )

    assert result["ok"] is False
    assert result["bad_sectors"] != []
    assert calls["n"] == len(result["bad_sectors"])


def test_verify_cancel(tmp_path, monkeypatch):
    _, device, _ = _write_pair(tmp_path, 300_000, seed=13)
    calls = {"n": 0}
    real = verify._read_chunk

    def counting(handle, buffer, count, retries, is_cancelled):
        calls["n"] += 1
        return real(handle, buffer, count, retries, is_cancelled)

    monkeypatch.setattr(verify, "_read_chunk", counting)
    result = verify.verify_device(
        str(device),
        chunk_size=64 * 1024,
        is_cancelled=lambda: calls["n"] > 1,
    )

    assert result["ok"] is False
    assert result["error"] == "cancelled"


def test_scan_bad_sectors_clean_device(tmp_path):
    _, device, _ = _write_pair(tmp_path, 150_000, seed=14)
    result = verify.scan_bad_sectors(str(device), chunk_size=64 * 1024)
    assert result["ok"] is True
    assert result["bad_sectors"] == []
    assert result["speed_mbps"] > 0


def test_scan_bad_sectors_reports_failures(tmp_path, monkeypatch):
    _, device, _ = _write_pair(tmp_path, 150_000, seed=15)

    def always_fail(handle, buffer, count, retries, is_cancelled):
        return None

    monkeypatch.setattr(verify, "_read_chunk", always_fail)
    result = verify.scan_bad_sectors(str(device), chunk_size=64 * 1024)
    assert result["ok"] is False
    assert result["bad_sectors"] != []


# ---------------------------------------------------------------- UI --------


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    import core.settings as s

    original = s.SETTINGS_PATH
    s.SETTINGS_PATH = tmp_path / "s.json"
    s._CACHE = None
    yield
    s._CACHE = None
    s.SETTINGS_PATH = original


def _make_window(qapp, tmp_path):
    import core.settings as s

    s.set_many(onboarding_seen=True, expert_mode=False, theme="dark")
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


def test_verify_options_gated_by_verify_toggle(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        w._verify_toggle.setChecked(False)
        assert not w._verify_sha_toggle.isEnabled()
        assert not w._bad_block_toggle.isEnabled()
        w._verify_toggle.setChecked(True)
        assert w._verify_sha_toggle.isEnabled()
        assert w._bad_block_toggle.isEnabled()
        assert not w._bad_retries_input.isEnabled()
        w._bad_block_toggle.setChecked(True)
        assert w._bad_retries_input.isEnabled()
        w._bad_block_toggle.setChecked(False)
        assert not w._bad_retries_input.isEnabled()
        assert s.get("verify_sha256") is True
        assert s.get("bad_block_scan") is False
    finally:
        w._shutdown()


def test_verify_options_persist_and_parse_retries(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        w._verify_toggle.setChecked(True)
        w._verify_sha_toggle.setChecked(False)
        w._bad_block_toggle.setChecked(True)
        w._bad_retries_input.setText("7")
        w._on_verify_options_changed()
        assert s.get("verify_sha256") is False
        assert s.get("bad_block_scan") is True
        assert s.get("bad_block_retries") == 7
        w._bad_retries_input.setText("99")
        assert w._bad_block_retries_value() == 10
        w._bad_retries_input.setText("banana")
        assert w._bad_block_retries_value() == 3
    finally:
        w._shutdown()


def test_verify_options_defaults_off_when_verify_unchecked(qapp, tmp_path):
    import core.settings as s

    s.set_many(verify_after_write=False)
    w = _make_window(qapp, tmp_path)
    try:
        assert not w._verify_toggle.isChecked()
        assert w._verify_sha_toggle.isChecked()  # default compare mode
    finally:
        w._shutdown()
