"""SHA-256 sidecar validation.

Flint can validate an image against a `*.sha256` file sitting next to it
before any destructive action. Two layouts are recognized:

    <image>.sha256       (e.g. ubuntu.iso.sha256)
    <stem>.sha256        (e.g. ubuntu.sha256)

The sidecar body may be any text containing a 64-hex digest, which covers
the common `sha256sum` format, certutil output and bare digests.

A sidecar may also list several files (``sha256sum *.iso``). When the
image name is known, a line whose filename column names a *different*
file is never used for this image; digest-only lines (certutil, bare
digests) have no filename column and are accepted.
"""

import re
from pathlib import Path

_HEX64 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")


def _basename(name: str) -> str:
    """Last path component, accepting both Windows and POSIX separators."""
    return name.strip().replace("\\", "/").rsplit("/", 1)[-1]


def find_sidecar(
    image_path: str | Path, *, sidecar_dir: str | Path | None = None
) -> Path | None:
    """Locate a sidecar checksum file for ``image_path``, or None.

    ``sidecar_dir`` looks only inside that directory, keeping the image
    name.  It exists for compressed selections: the payload is extracted
    into a temp folder while its sidecar lives next to the original
    archive, so the directory (not the extracted path) has to be
    overridden.
    """
    path = Path(image_path)
    if sidecar_dir is not None:
        path = Path(sidecar_dir) / path.name
    candidates = (
        Path(str(path) + ".sha256"),
        path.with_suffix(path.suffix + ".sha256"),
        Path(str(path.with_suffix("")) + ".sha256"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def parse_sidecar(
    text: str, image_name: str | None = None
) -> str | None:
    """Extract a 64-hex digest from sidecar text, lowercased, or None.

    Without ``image_name`` the first digest anywhere in the file wins
    (historical behaviour).  With it, only lines that either name this
    image or carry no filename column at all are considered, so a sidecar
    listing several files cannot be mistaken for this image's digest.
    """
    fallback: str | None = None
    wanted = _basename(image_name).casefold() if image_name else None
    for line in text.splitlines():
        match = _HEX64.search(line)
        if not match:
            continue
        if wanted is None:
            return match.group(0).lower()
        # Text around the digest: for `sha256sum` output the remainder is
        # the filename column ("digest  name" / "digest *name").
        rest = (line[: match.start()] + line[match.end() :]).strip(
            " \t\r\n*\ufeff"
        )
        if not rest:
            # No filename column (certutil output, bare digest): usable
            # for any image, but only if no explicit match shows up.
            if fallback is None:
                fallback = match.group(0).lower()
            continue
        if _basename(rest).casefold() == wanted:
            return match.group(0).lower()
        # Explicit filename for a different file: never use this digest.
    return fallback


def sidecar_digest(
    sidecar: Path, image_name: str | None = None
) -> tuple[bool, str]:
    """Read a sidecar file and return ``(True, digest)`` or ``(False,
    message)`` when it cannot be read or holds no digest.

    Pass ``image_name`` to ignore entries that belong to other files.
    """
    try:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"could not read {sidecar.name}: {exc}"
    digest = parse_sidecar(text, image_name)
    if digest is None:
        if image_name and parse_sidecar(text) is not None:
            return (
                False,
                f"{sidecar.name} lists no SHA-256 digest for {image_name}",
            )
        return False, f"{sidecar.name} contains no SHA-256 digest"
    return True, digest


def check_sidecar(
    image_path: str | Path,
    actual_digest: str | None,
    *,
    sidecar_dir: str | Path | None = None,
) -> tuple[str, str]:
    """Compare a computed image digest against the sidecar (if any).

    Returns ``(status, detail)`` where status is one of:

    - ``"missing"``   no sidecar file exists next to the image
    - ``"pending"``   sidecar found but no computed digest yet
    - ``"ok"``        computed digest matches the sidecar
    - ``"mismatch"``  computed digest differs (corrupt or wrong image)
    - ``"error"``     sidecar exists but cannot be read, holds no digest,
                      or lists none for this image

    See :func:`find_sidecar` for ``sidecar_dir``.
    """
    sidecar = find_sidecar(image_path, sidecar_dir=sidecar_dir)
    if sidecar is None:
        return "missing", ""
    ok, detail = sidecar_digest(sidecar, Path(image_path).name)
    if not ok:
        return "error", detail
    if actual_digest is None:
        return "pending", sidecar.name
    if actual_digest.lower() == detail:
        return "ok", sidecar.name
    return "mismatch", sidecar.name
