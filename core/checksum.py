"""SHA-256 sidecar validation.

Flint can validate an image against a `*.sha256` file sitting next to it
before any destructive action. Two layouts are recognized:

    <image>.sha256       (e.g. ubuntu.iso.sha256)
    <stem>.sha256        (e.g. ubuntu.sha256)

The sidecar body may be any text containing a 64-hex digest, which covers
the common `sha256sum` format, certutil output and bare digests.
"""

import re
from pathlib import Path

_HEX64 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")


def find_sidecar(image_path: str | Path) -> Path | None:
    """Locate a sidecar checksum file for ``image_path``, or None."""
    path = Path(image_path)
    candidates = (
        Path(str(path) + ".sha256"),
        path.with_suffix(path.suffix + ".sha256"),
        Path(str(path.with_suffix("")) + ".sha256"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def parse_sidecar(text: str) -> str | None:
    """Extract the first 64-hex digest from sidecar text, lowercased, or
    None when the file contains no digest at all."""
    for line in text.splitlines():
        match = _HEX64.search(line)
        if match:
            return match.group(0).lower()
    return None


def sidecar_digest(sidecar: Path) -> tuple[bool, str]:
    """Read a sidecar file and return ``(True, digest)`` or ``(False,
    message)`` when it cannot be read or holds no digest."""
    try:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"could not read {sidecar.name}: {exc}"
    digest = parse_sidecar(text)
    if digest is None:
        return False, f"{sidecar.name} contains no SHA-256 digest"
    return True, digest


def check_sidecar(
    image_path: str | Path, actual_digest: str | None
) -> tuple[str, str]:
    """Compare a computed image digest against the sidecar (if any).

    Returns ``(status, detail)`` where status is one of:

    - ``"missing"``   no sidecar file exists next to the image
    - ``"pending"``   sidecar found but no computed digest yet
    - ``"ok"``        computed digest matches the sidecar
    - ``"mismatch"``  computed digest differs (corrupt or wrong image)
    - ``"error"``     sidecar exists but cannot be read or has no digest
    """
    sidecar = find_sidecar(image_path)
    if sidecar is None:
        return "missing", ""
    ok, detail = sidecar_digest(sidecar)
    if not ok:
        return "error", detail
    if actual_digest is None:
        return "pending", sidecar.name
    if actual_digest.lower() == detail:
        return "ok", sidecar.name
    return "mismatch", sidecar.name
