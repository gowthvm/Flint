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


# ---------------------------------------------------------------------------
# B11: a sidecar listing several files must be read per-image
# ---------------------------------------------------------------------------

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def test_parse_picks_the_line_that_names_this_image():
    text = (
        f"{_DIGEST_A}  other.iso\n"
        f"{_DIGEST_B}  ubuntu.iso\n"
        f"{_DIGEST_A}  third.iso\n"
    )
    assert checksum.parse_sidecar(text, "ubuntu.iso") == _DIGEST_B


def test_parse_ignores_a_line_for_a_different_file():
    text = f"{_DIGEST_A}  other.iso\n"
    assert checksum.parse_sidecar(text, "ubuntu.iso") is None
    # Without a name the historical "first digest" behaviour remains.
    assert checksum.parse_sidecar(text) == _DIGEST_A


def test_parse_matches_basename_of_a_full_path():
    text = f"{_DIGEST_B}  C:\\dist\\UBUNTU.ISO\n"
    assert checksum.parse_sidecar(text, "ubuntu.iso") == _DIGEST_B
    text = f"{_DIGEST_B}  ./build/ubuntu.iso\n"
    assert checksum.parse_sidecar(text, r"C:\x\ubuntu.iso") == _DIGEST_B


def test_parse_accepts_binary_mode_marker():
    text = f"{_DIGEST_B} *ubuntu.iso\n"
    assert checksum.parse_sidecar(text, "ubuntu.iso") == _DIGEST_B


def test_parse_accepts_digest_only_lines_when_name_given():
    text = f"SHA256 hash of ubuntu.iso:\r\n{_DIGEST_B.upper()}\r\n"
    assert checksum.parse_sidecar(text, "ubuntu.iso") == _DIGEST_B


def test_parse_accepts_bare_digest_when_name_given():
    assert checksum.parse_sidecar(_DIGEST_B, "ubuntu.iso") == _DIGEST_B


def test_sidecar_digest_says_when_no_entry_is_for_this_image(tmp_path):
    sidecar = tmp_path / "ubuntu.iso.sha256"
    sidecar.write_text(f"{_DIGEST_A}  other.iso\n")

    ok, message = checksum.sidecar_digest(sidecar, "ubuntu.iso")

    assert ok is False
    assert "no SHA-256 digest for ubuntu.iso" in message


def test_check_sidecar_ignores_other_files_entry(tmp_path):
    image = tmp_path / "img.iso"
    (tmp_path / "img.iso.sha256").write_text(
        f"{_DIGEST_A}  other.iso\n{_DIGEST_B}  img.iso\n"
    )

    assert checksum.check_sidecar(image, _DIGEST_B)[0] == "ok"
    assert checksum.check_sidecar(image, _DIGEST_A)[0] == "mismatch"


def test_check_sidecar_errors_when_only_other_file_listed(tmp_path):
    image = tmp_path / "img.iso"
    (tmp_path / "img.iso.sha256").write_text(f"{_DIGEST_A}  other.iso\n")

    status, detail = checksum.check_sidecar(image, _DIGEST_A)

    assert status == "error"
    assert "for img.iso" in detail


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