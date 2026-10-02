"""Update feed parsing, version comparison, verified downloads and the
7-day auto-check policy. Network calls are faked via monkeypatched
``urlopen`` so no test touches the network."""

import hashlib
import time
import urllib.error
from pathlib import Path

from core import updates
from core.version import APP_VERSION

RELEASE = {
    "tag_name": "v1.5.0",
    "name": "Flint 1.5.0",
    "assets": [
        {
            "name": "flint.exe",
            "browser_download_url": "https://example.test/flint.exe",
            "size": 1234,
        },
        {
            "name": "flint.exe.sha256",
            "browser_download_url": "https://example.test/flint.exe.sha256",
        },
    ],
}


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._payload
        chunk = self._payload[:n]
        self._payload = self._payload[n:]
        return chunk


def _fake_urlopen(payload: bytes, status: int = 200, headers: dict | None = None):
    def _open(request, timeout=None):
        if status == 404:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, None
            )
        if status == 500:
            raise urllib.error.URLError("boom")
        return _FakeResponse(payload, headers)

    return _open


def test_fetch_latest_parses_release(monkeypatch):
    import json
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(json.dumps(RELEASE).encode()),
    )
    ok, data = updates.fetch_latest("https://example.test/latest")
    assert ok
    assert data["tag_name"] == "v1.5.0"


def test_fetch_latest_private_repo_message(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"", 404))
    ok, message = updates.fetch_latest("https://example.test/latest")
    assert not ok
    assert "private" in message


def test_fetch_latest_network_error(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"", 500))
    ok, _message = updates.fetch_latest("https://example.test/latest")
    assert not ok


def test_release_executable_finds_asset():
    asset = updates.release_executable(RELEASE)
    assert asset is not None and asset["name"] == "flint.exe"
    assert updates.release_executable({"assets": []}) is None


def test_compare_version():
    assert updates.compare_version("1.0.1", "1.0.1") == 0
    assert updates.compare_version("1.0.1", "v1.0.2") == -1
    assert updates.compare_version("1.0.1", "1.0.0") == 1
    assert updates.compare_version("1.0.1", "1.10.0") == -1
    assert updates.compare_version("1.0.1", "1.0.1rc1") == 0


def test_version_from_tag():
    assert updates.version_from_tag("v1.5.0") == "1.5.0"
    assert updates.version_from_tag("1.5") == "1.5"


def test_should_auto_check():
    assert updates.should_auto_check(None)
    assert not updates.should_auto_check(time.time())
    assert updates.should_auto_check(time.time() - 8 * 86400)


def test_download_and_verify_ok(tmp_path, monkeypatch):
    import urllib.request

    payload = b"flint-exe-bytes" * 1000
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(payload, headers={"Content-Length": str(len(payload))}),
    )
    dest = tmp_path / "flint.exe"
    progress: list[tuple[int, int]] = []

    ok, result = updates.download_and_verify(
        "https://example.test/flint.exe",
        dest,
        digest,
        progress=lambda d, t: progress.append((d, t)),
    )

    assert ok
    assert result == digest
    assert dest.read_bytes() == payload
    assert progress and progress[-1][0] == len(payload)


def test_download_and_verify_mismatch_removes_file(tmp_path, monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(b"payload", headers={"Content-Length": "7"}),
    )
    dest = tmp_path / "flint.exe"

    ok, message = updates.download_and_verify(
        "https://example.test/flint.exe", dest, "0" * 64
    )

    assert not ok
    assert "SHA-256" in message
    assert not dest.exists()


def test_download_and_verify_network_failure_removes_file(
    tmp_path, monkeypatch
):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"", 500))
    dest = tmp_path / "flint.exe"
    dest.write_bytes(b"partial")
    ok, _message = updates.download_and_verify(
        "https://example.test/flint.exe", dest, None
    )
    assert not ok
    assert not dest.exists()


def test_download_and_verify_rejects_truncated_stream_without_digest(
    tmp_path, monkeypatch
):
    """A dropped connection reads as EOF, not as an error.

    With no expected digest (missing sidecar, or the sidecar fetch failed)
    the short stream used to be reported as a successful download of a
    partial executable.
    """
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(
            b"half of it", headers={"Content-Length": str(10 * 1024 * 1024)}
        ),
    )
    dest = tmp_path / "flint.exe"

    ok, message = updates.download_and_verify(
        "https://example.test/flint.exe", dest, None
    )

    assert not ok
    assert "incomplete" in message
    assert not dest.exists()


def test_download_and_verify_rejects_truncated_stream_even_with_digest(
    tmp_path, monkeypatch
):
    """The digest of the bytes that arrived is not proof they are all here."""
    import urllib.request

    payload = b"truncated"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(payload, headers={"Content-Length": str(len(payload) + 1)}),
    )
    dest = tmp_path / "flint.exe"

    ok, message = updates.download_and_verify(
        "https://example.test/flint.exe", dest, digest
    )

    assert not ok
    assert "incomplete" in message
    assert not dest.exists()


def test_download_and_verify_accepts_complete_stream_without_digest(
    tmp_path, monkeypatch
):
    """No digest must not mean 'reject everything' - only short reads."""
    import urllib.request

    payload = b"flint-exe-bytes" * 64
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(payload, headers={"Content-Length": str(len(payload))}),
    )
    dest = tmp_path / "flint.exe"

    ok, _digest = updates.download_and_verify(
        "https://example.test/flint.exe", dest, None
    )

    assert ok
    assert dest.read_bytes() == payload


def test_sidecar_digest_url():
    assert (
        updates.sidecar_digest_url(RELEASE)
        == "https://example.test/flint.exe.sha256"
    )
    assert updates.sidecar_digest_url({"assets": []}) is None


def test_fetch_sidecar_digest(monkeypatch):
    import urllib.request

    digest = "ab" * 32
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(digest.encode()))
    assert updates.fetch_sidecar_digest("https://example.test/x") == digest


def test_fetch_sidecar_digest_junk(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"not-a-checksum"))
    assert updates.fetch_sidecar_digest("https://example.test/x") is None


def test_fetch_sidecar_digest_sha256sum_format(monkeypatch):
    """A sidecar that also carries a filename column must still be usable.

    The old "concatenate every hex character" parser returned 67 chars for
    ``<digest>  flint.exe`` and fell back to ``None`` - which silently
    disabled the digest check on the download even though the release had
    published a checksum.
    """
    import urllib.request

    digest = "cd" * 32
    body = f"{digest}  flint.exe\r\n".encode("ascii")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(body))
    assert updates.fetch_sidecar_digest("https://example.test/x") == digest


def test_fetch_sidecar_digest_certutil_format(monkeypatch):
    import urllib.request

    digest = "ef" * 32
    body = f"SHA256(flint.exe)= {digest}\r\n".encode("ascii")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(body))
    assert updates.fetch_sidecar_digest("https://example.test/x") == digest


def test_fetch_sidecar_digest_missing_url():
    assert updates.fetch_sidecar_digest(None) is None


def test_default_download_path(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()
    path = Path(updates.default_download_path("v1.5.0"))
    assert path.name == "flint-1.5.0.exe"
    assert path.parent == tmp_path / "Downloads"


def test_app_version_constant_is_well_formed():
    assert APP_VERSION.count(".") == 2
    parts = APP_VERSION.split(".")
    assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# T1: MainWindow._shutdown() calls .cancel() on every live worker
# ---------------------------------------------------------------------------


def test_every_update_worker_exposes_cancel():
    for worker in (
        updates.UpdateCheckWorker,
        updates.UpdateDownloadWorker,
        updates.DigestFetchWorker,
    ):
        assert callable(getattr(worker, "cancel", None)), worker.__name__


def test_download_and_verify_cancel_removes_partial_file(tmp_path, monkeypatch):
    import urllib.request

    # Several 256 KiB chunks so the cancel flag is observed between reads.
    payload = b"x" * (512 * 1024 + 10)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _fake_urlopen(payload, headers={"Content-Length": str(len(payload))}),
    )
    dest = tmp_path / "flint.exe"
    cancelled = {"flag": False}

    def on_progress(done: int, total: int) -> None:
        cancelled["flag"] = True  # the user hits cancel mid-download

    ok, message = updates.download_and_verify(
        "https://example.test/flint.exe",
        dest,
        None,
        progress=on_progress,
        is_cancelled=lambda: cancelled["flag"],
    )

    assert not ok
    assert message == "cancelled"
    assert not dest.exists()  # never leave a half-written exe behind