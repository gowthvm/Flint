"""SHA-256 sidecar discovery, parsing and validation."""

from core import checksum


def test_find_sidecar_after_extension(tmp_path):
    image = tmp_path / "ubuntu.iso"
    sidecar = tmp_path / "ubuntu.iso.sha256"
    sidecar.write_text("abc\n")
    assert image.exists() is False  # sidecar can exist before the image copy
    found = checksum.find_sidecar(image)
    assert found == sidecar


def test_find_sidecar_stem_layout(tmp_path):
    image = tmp_path / "ubuntu.iso"
    sidecar = tmp_path / "ubuntu.sha256"
    sidecar.write_text("d\n")
    assert checksum.find_sidecar(image) == sidecar


def test_find_sidecar_prefers_suffix_layout(tmp_path):
    image = tmp_path / "ubuntu.iso"
    (tmp_path / "ubuntu.sha256").write_text("a\n")
    (tmp_path / "ubuntu.iso.sha256").write_text("b\n")
    assert checksum.find_sidecar(image).name == "ubuntu.iso.sha256"


def test_find_sidecar_missing(tmp_path):
    assert checksum.find_sidecar(tmp_path / "none.iso") is None


def test_parse_sha256sum_format():
    text = "d56f3c5b1b6d2b2fd0d652b4e076f7a1e2be8586e1a1e8c6b33609445e6c9f3d  ubuntu.iso\n"
    assert checksum.parse_sidecar(text).startswith("d56f3c5b")


def test_parse_certutil_format():
    digest = "abcdef0123456789" * 4  # exactly 64 hex chars
    assert len(digest) == 64
    text = f"SHA256 hash of SOME_Path\\ubuntu.iso:\r\n{digest.upper()}\r\n"
    assert checksum.parse_sidecar(text) == digest


def test_parse_sidecar_bare_digest():
    digest = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert checksum.parse_sidecar(digest) == digest


def test_parse_sidecar_junk_returns_none():
    assert checksum.parse_sidecar("no digest here\nnothing\n") is None


def test_sidecar_digest_unreadable(tmp_path):
    sidecar = tmp_path / "x.sha256"
    ok, message = checksum.sidecar_digest(sidecar)
    assert ok is False
    assert message.startswith("could not read x.sha256")


def test_check_sidecar_states(tmp_path):
    image = tmp_path / "img.iso"
    digest = "a" * 64

    assert checksum.check_sidecar(image, None) == ("missing", "")

    (tmp_path / "img.iso.sha256").write_text(f"{digest}  img.iso\n")
    assert checksum.check_sidecar(image, None) == ("pending", "img.iso.sha256")
    assert checksum.check_sidecar(image, digest.upper())[0] == "ok"
    assert checksum.check_sidecar(image, "b" * 64)[0] == "mismatch"

    (tmp_path / "img.iso.sha256").write_text("not a checksum\n")
    status, detail = checksum.check_sidecar(image, "a" * 64)
    assert status == "error"
    assert "no SHA-256 digest" in detail