"""Headless / scriptable mode.

Usage (all commands require elevation, which the packaged executable has
via its manifest; in development a UAC relaunch is attempted):

    flint --cli list
    flint --cli flash  --image <file> --drive <serial|letter|path> --confirm <serial> [--verify]
    flint --cli verify --drive <serial|letter|path> [--sha256 <hex> --image <file>]
    flint --cli wipe   --drive <serial|letter|path> --confirm <serial> [--method zero|random|nist|dod]
    flint --cli backup --drive <serial|letter|path> --out <file> [--confirm <serial>]
    flint --cli clone  --from <serial|letter|path> --to <serial|letter|path> --confirm <serial of --to>
    flint --cli queue  --file <list.txt> --drive <serial|letter|path> --confirm <serial>

    flint --cli help

``list`` prints every detected drive with its serial, volume letter(s)
and size, and needs no privileges; the serial it prints is exactly what
subsequent ``--confirm`` values must match.

``--confirm`` must equal the *full* serial number of the drive being
destroyed; this is the headless equivalent of the GUI's typed
confirmation. Commands are validated against the live drive list, so a
wrong serial can never match another drive.

``verify`` runs a read-only bad-block scan of the whole drive when no
digest is given; with ``--sha256`` the digest covers only the image
bytes, so ``--image <file>`` is required to know how many bytes to
compare (a whole drive is usually larger than the flashed image and
would never match the image digest).

Output is line-oriented: progress lines ``FLINT <pct> <speed>MB/s ETA <s>s``
plus a final ``RESULT ok|fail|canceled: <message>``. Exit codes:

    0 ok, 1 failure, 2 cancelled, 3 usage/validation, 4 elevation denied
"""

import ctypes
import os
import subprocess
import sys
import time
from typing import Any

from PyQt6.QtCore import QCoreApplication, QEventLoop

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CANCELLED = 2
EXIT_USAGE = 3
EXIT_NO_ADMIN = 4

_VALUE_OPTS = {
    "image",
    "drive",
    "out",
    "file",
    "method",
    "confirm",
    "from",
    "to",
    "sha256",
}
_FLAG_OPTS = {"verify"}

_METHODS = ("zero", "random", "nist", "dod")


def _print(line: str) -> None:
    try:
        print(line, flush=True)
    except OSError:
        pass


def _ensure_cli_stdio() -> None:
    """Give the packaged (windowed) build real stdio for CLI mode.

    PyInstaller builds flint with ``console=False``, so the GUI never
    flashes a console. CLI invocations launched from PowerShell or
    Explorer then have no stdout handle at all and ``print`` raises
    OSError under Python 3.14's stricter stdio handling. When the std
    handles are not real files or pipes (cmd-style redirection), attach
    to the parent console instead so CLI output is visible. In dev there
    is a real console and nothing happens.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import msvcrt

        for stream in (sys.stdout, sys.stderr):
            fd = stream.fileno()
            handle = msvcrt.get_osfhandle(fd)
            if handle != -1 and ctypes.windll.kernel32.GetFileType(handle) in (
                1,
                3,
            ):
                return
    except Exception:
        pass
    try:
        # ATTACH_PARENT_PROCESS: reuse the console of the process that
        # launched us, then reopen the standard streams onto it.
        if ctypes.windll.kernel32.AttachConsole(0xFFFFFFFF):
            # These streams must outlive the function; they become
            # sys.stdout/stderr/stdin for the rest of the process.
            sys.stdout = open(  # noqa: SIM115
                "CONOUT$", "w", encoding="utf-8", errors="replace"
            )
            sys.stderr = open(  # noqa: SIM115
                "CONOUT$", "w", encoding="utf-8", errors="replace"
            )
            sys.stdin = open(  # noqa: SIM115
                "CONIN$", "r", encoding="utf-8", errors="replace"
            )
    except Exception:
        pass


def _opts(argv: list[str]) -> tuple[dict[str, str | bool], str | None]:
    """Parse ``--name value`` pairs; return ``(opts, error)``."""
    opts: dict[str, str | bool] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            return {}, f"unexpected argument: {arg}"
        name = arg[2:]
        if name in _VALUE_OPTS:
            if i + 1 >= len(argv):
                return {}, f"missing value for --{name}"
            opts[name] = argv[i + 1]
            i += 2
        elif name in _FLAG_OPTS or name in ("cli", "help"):
            opts[name] = True
            i += 1
        else:
            return {}, f"unknown option: --{name}"
    return opts, None


def _usage() -> str:
    return (
        "Usage: flint --cli <command> [options]\n"
        "  list   [no options] — print detected drives and their serials\n"
        "  flash  --image <file> --drive <serial|letter|path>"
        " --confirm <serial> [--verify]\n"
        "  verify --drive <serial|letter|path>"
        " [--sha256 <hex> --image <file>]\n"
        "  wipe   --drive <serial|letter|path> --confirm <serial>"
        " [--method zero|random|nist|dod]\n"
        "  backup --drive <serial|letter|path> --out <file>"
        " [--confirm <serial>]\n"
        "  clone  --from <serial|letter|path> --to <serial|letter|path>"
        " --confirm <serial of --to>\n"
        "  queue  --file <list.txt> --drive <serial|letter|path>"
        " --confirm <serial>\n"
    )


def ensure_elevated(argv: list[str]) -> int | None:
    """Return None when already elevated (or elevation is unavailable),
    otherwise relaunch elevated and return the child's exit code."""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return None
    except Exception:
        return None
    _print("administrator privileges required - relaunching elevated...")
    args = subprocess.list2cmdline(argv)
    command = (
        "$p = Start-Process -FilePath '"
        + sys.executable.replace("'", "''")
        + "' -ArgumentList '"
        + args.replace("'", "''")
        + "' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        _print(f"could not relaunch elevated: {exc}")
        return EXIT_NO_ADMIN
    if proc.returncode != 0:
        _print("elevation denied or failed (UAC prompt not accepted?)")
        return EXIT_NO_ADMIN
    try:
        return int((proc.stdout or "").strip() or EXIT_OK)
    except ValueError:
        return EXIT_OK


def _detect_drives() -> list[dict[str, Any]]:
    from core.drives import DriveDetector

    detector = DriveDetector()
    return detector.list_removable_drives()


def _resolve_drive(
    spec: str, drives: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Match a drive by physical path, serial, or volume letter."""
    if not drives:
        return None
    lowered = spec.casefold()
    for drive in drives:
        if spec == drive.get("physical_path"):
            return drive
        serial = (drive.get("serial") or "").casefold()
        if serial and serial == lowered:
            return drive
        letters = drive.get("letters") or (
            [drive["letter"]] if drive.get("letter") else []
        )
        if any(l.casefold() == lowered.strip(":") for l in letters):
            return drive
    return None


def _serial_of(drive: dict[str, Any]) -> str:
    return drive.get("serial") or drive.get("name") or ""


def _require_confirm(
    drive: dict[str, Any], confirmed: str | None
) -> str | None:
    if not confirmed:
        return "this command destroys a drive: pass --confirm <full serial>"
    serial = _serial_of(drive).casefold()
    if not serial:
        return "drive has no serial to confirm against"
    if confirmed.casefold() != serial:
        return (
            f"--confirm {confirmed!r} does not match the drive serial "
            f"{_serial_of(drive)!r}"
        )
    return None


def _run_worker(worker: Any, label: str) -> tuple[bool, str]:
    """Run a QThread worker to completion, streaming progress lines.

    Emits ``FLINT <pct> <speed>MB/s ETA <s>s`` on every worker signal and
    at 100%; speed/ETA come from the worker's own signals when it provides
    them (writer/wipe/backup/clone), or are derived from reported bytes,
    or are reported as 0 / remaining-seconds when unavailable (verify).
    """
    loop = QEventLoop()
    outcome: dict[str, object] = {}
    state: dict[str, float] = {
        "pct": 0.0,
        "speed": 0.0,
        "written": 0.0,
        "total": 0.0,
    }
    stated_at = time.monotonic()
    last_print = {"t": 0.0}

    def _emit() -> None:
        elapsed = max(time.monotonic() - stated_at, 1e-6)
        speed = state["speed"]
        eta = 0
        if speed <= 0 and state["total"] > 0 and state["written"] > 0:
            speed = state["written"] / 1_000_000 / elapsed
            remaining = state["total"] - state["written"]
            eta = int(remaining / 1_000_000 / speed) if speed > 0 else 0
        _print(f"FLINT {state['pct']:.1f} {speed:.1f}MB/s ETA {eta}s")
        last_print["t"] = time.monotonic()

    def on_progress(pct: float) -> None:
        if pct >= 100.0 or (
            pct > state["pct"]
            and time.monotonic() - last_print["t"] >= 0.25
        ):
            state["pct"] = pct
            _emit()

    def on_speed(mbps: float) -> None:
        state["speed"] = mbps

    def on_written(n: int) -> None:
        state["written"] = float(n)

    def on_total(n: int) -> None:
        state["total"] = float(n)

    def on_finished(ok: bool, message: str) -> None:
        outcome["ok"] = ok
        outcome["message"] = message
        loop.quit()

    worker.progress.connect(on_progress)
    speed_signal = getattr(worker, "speed_mbps", None)
    if speed_signal is not None:
        speed_signal.connect(on_speed)
    written_signal = getattr(worker, "written_bytes", None)
    if written_signal is not None:
        written_signal.connect(on_written)
    total_signal = getattr(worker, "total_bytes", None)
    if total_signal is not None:
        total_signal.connect(on_total)
    worker.finished.connect(on_finished)
    worker.start()
    loop.exec()
    return bool(outcome["ok"]), str(outcome.get("message", ""))


def _iso_digest(image: str) -> str | None:
    from core.verify import compute_sha256

    _print(f"hashing {image}...")
    ok, result = compute_sha256(image)
    if not ok:
        _print(f"RESULT fail: could not hash image: {result}")
        return None
    _print(f"SHA256 {result}")
    return result


def _cmd_list(opts: dict[str, str | bool]) -> int:
    """Print every detected drive with the serial that --confirm expects.

    Needs no privileges, so scripts can discover serials first and then
    run destructive commands with the exact value."""
    drives = _detect_drives()
    if not drives:
        _print("RESULT ok: no removable drives detected")
        return EXIT_OK
    for index, drive in enumerate(drives, 1):
        model = drive.get("name") or drive.get("model") or "unknown device"
        letters = drive.get("letters") or (
            [drive["letter"]] if drive.get("letter") else []
        )
        serial = _serial_of(drive)
        _print(
            f"DRIVE {index} {model} "
            f"serial={serial!r} "
            f"size={drive.get('size_gb', 0)}GB "
            f"letters={','.join(letters)} "
            f"path={drive.get('physical_path')}"
        )
    _print(f"RESULT ok: {len(drives)} drive(s) listed")
    return EXIT_OK


def _cmd_flash(opts: dict[str, str | bool]) -> int:
    from core.writer import UsbWriter

    image = str(opts.get("image", ""))
    if not os.path.isfile(image):
        _print("RESULT fail: --image file not found")
        return EXIT_USAGE
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        _print("RESULT fail: drive not found; detected drives:")
        for d in drives:
            _print(
                f"  serial={_serial_of(d)!r} "
                f"path={d.get('physical_path')} "
                f"letters={d.get('letters')}"
            )
        return EXIT_USAGE
    issue = _require_confirm(drive, str(opts.get("confirm", "")))
    if issue:
        _print(f"RESULT fail: {issue}")
        return EXIT_USAGE
    image_size = os.path.getsize(image)
    if image_size > (drive.get("size_gb", 0) * 1_000_000_000):
        _print("RESULT fail: image is larger than the target drive")
        return EXIT_FAIL
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = UsbWriter(
        image,
        drive["physical_path"],
        letters=letters,
        verify_after_write=False,
    )
    ok, message = _run_worker(worker, "flash")
    if not ok:
        _print(f"RESULT {('canceled' if message == 'cancelled' else 'fail')}: {message}")
        return EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL
    if not opts.get("verify"):
        _print("RESULT ok: flashed")
        return EXIT_OK
    digest = _iso_digest(image)
    if digest is None:
        return EXIT_FAIL
    return _cmd_verify_raw(
        drive["physical_path"], image_size, digest, letters
    )


def _cmd_verify_raw(
    path: str, size: int, expected: str, letters: list[str]
) -> int:
    from core.verify import VerifyWorker

    worker = VerifyWorker(path, expected, size)
    ok, message = _run_worker(worker, "verify")
    if not ok:
        _print(f"RESULT fail: {message}")
        return EXIT_FAIL
    _print("RESULT ok: verification passed")
    return EXIT_OK


def _cmd_verify(opts: dict[str, str | bool]) -> int:
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        _print("RESULT fail: drive not found")
        return EXIT_USAGE
    expected = str(opts.get("sha256", ""))
    if expected:
        if len(expected) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in expected
        ):
            _print("RESULT fail: --sha256 must be a 64-hex digest")
            return EXIT_USAGE
        # The digest covers only the image bytes, so the drive must be
        # sized to the image (not the whole disk, which is usually larger
        # and would never match). Derive the byte count from --image.
        image = str(opts.get("image", ""))
        if not image:
            _print(
                "RESULT fail: --sha256 requires --image <file> so the "
                "byte range to verify is known"
            )
            return EXIT_USAGE
        if not os.path.isfile(image):
            _print("RESULT fail: --image file not found")
            return EXIT_USAGE
        size = os.path.getsize(image)
    else:
        # No digest: read-only bad-block scan of the whole drive.
        from core.verify import verify_device

        _print("scanning drive...")
        result = verify_device(drive["physical_path"])
        if not result.get("ok"):
            _print(
                f"RESULT fail: bad sectors: "
                f"{len(result['bad_sectors'])} ({result['error']})"
            )
            return EXIT_FAIL
        _print("RESULT ok: no bad sectors")
        return EXIT_OK
    return _cmd_verify_raw(
        drive["physical_path"], size, expected.lower(),
        drive.get("letters") or [],
    )


def _cmd_wipe(opts: dict[str, str | bool]) -> int:
    from core.wipe import WIPE_METHODS, WipeWorker

    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        _print("RESULT fail: drive not found")
        return EXIT_USAGE
    issue = _require_confirm(drive, str(opts.get("confirm", "")))
    if issue:
        _print(f"RESULT fail: {issue}")
        return EXIT_USAGE
    method = str(opts.get("method", "zero")).lower()
    if method not in WIPE_METHODS:
        _print(f"RESULT fail: --method must be one of {', '.join(WIPE_METHODS)}")
        return EXIT_USAGE
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = WipeWorker(drive["physical_path"], letters, method=method)
    ok, message = _run_worker(worker, "wipe")
    if not ok:
        _print(f"RESULT {('canceled' if message == 'cancelled' else 'fail')}: {message}")
        return EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL
    _print(f"RESULT ok: wiped ({method})")
    return EXIT_OK


def _cmd_backup(opts: dict[str, str | bool]) -> int:
    from core.backup import BackupWorker

    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        _print("RESULT fail: drive not found")
        return EXIT_USAGE
    out = str(opts.get("out", ""))
    if not out:
        _print("RESULT fail: --out <file> is required")
        return EXIT_USAGE
    confirm = str(opts.get("confirm", "")) if opts.get("confirm") else None
    if confirm:
        issue = _require_confirm(drive, confirm)
        if issue:
            _print(f"RESULT fail: {issue}")
            return EXIT_USAGE
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = BackupWorker(drive["physical_path"], out, letters=letters)
    ok, message = _run_worker(worker, "backup")
    if not ok:
        _print(f"RESULT {('canceled' if message == 'cancelled' else 'fail')}: {message}")
        return EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL
    _print(f"RESULT ok: backup saved to {out}")
    return EXIT_OK


def _cmd_clone(opts: dict[str, str | bool]) -> int:
    from core.clone import CloneWorker

    drives = _detect_drives()
    source = _resolve_drive(str(opts.get("from", "")), drives)
    target = _resolve_drive(str(opts.get("to", "")), drives)
    if source is None or target is None:
        _print("RESULT fail: source or target drive not found")
        return EXIT_USAGE
    if source.get("physical_path") == target.get("physical_path"):
        _print("RESULT fail: source and target are the same drive")
        return EXIT_USAGE
    issue = _require_confirm(target, str(opts.get("confirm", "")))
    if issue:
        _print(f"RESULT fail: {issue}")
        return EXIT_USAGE
    if (target.get("size_gb", 0) * 1_000_000_000) < (
        source.get("size_gb", 0) * 1_000_000_000
    ):
        _print("RESULT fail: target drive is smaller than the source")
        return EXIT_USAGE
    worker = CloneWorker(
        source["physical_path"],
        target["physical_path"],
        source_letters=source.get("letters") or [],
        target_letters=target.get("letters") or [],
    )
    ok, message = _run_worker(worker, "clone")
    if not ok:
        _print(f"RESULT {('canceled' if message == 'cancelled' else 'fail')}: {message}")
        return EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL
    _print("RESULT ok: clone complete")
    return EXIT_OK


def _cmd_queue(opts: dict[str, str | bool]) -> int:
    """Flash every image listed in a file (one per line, # comments
    allowed) to the same drive, stopping on the first failure."""
    queue_file = str(opts.get("file", ""))
    if not os.path.isfile(queue_file):
        _print("RESULT fail: --file not found")
        return EXIT_USAGE
    images: list[str] = []
    try:
        with open(queue_file, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if not os.path.isfile(line):
                    _print(f"RESULT fail: queue item not found: {line}")
                    return EXIT_USAGE
                images.append(line)
    except OSError as exc:
        _print(f"RESULT fail: could not read queue: {exc}")
        return EXIT_USAGE
    if not images:
        _print("RESULT fail: queue file has no images")
        return EXIT_USAGE
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        _print("RESULT fail: drive not found")
        return EXIT_USAGE
    issue = _require_confirm(drive, str(opts.get("confirm", "")))
    if issue:
        _print(f"RESULT fail: {issue}")
        return EXIT_USAGE
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    for index, image in enumerate(images, 1):
        _print(f"--- queue {index}/{len(images)}: {os.path.basename(image)}")
        from core.writer import UsbWriter

        worker = UsbWriter(image, drive["physical_path"], letters=letters)
        ok, message = _run_worker(worker, "flash")
        if not ok:
            _print(
                f"RESULT {'canceled' if message == 'cancelled' else 'fail'}: "
                f"queue stopped at {image} ({message})"
            )
            return EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL
    _print(f"RESULT ok: queue complete ({len(images)} images)")
    return EXIT_OK


_COMMANDS = {
    "list": _cmd_list,
    "flash": _cmd_flash,
    "verify": _cmd_verify,
    "wipe": _cmd_wipe,
    "backup": _cmd_backup,
    "clone": _cmd_clone,
    "queue": _cmd_queue,
}


def main(argv: list[str] | None = None) -> int:
    _ensure_cli_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--cli" in argv:
        argv.remove("--cli")
    if "--version" in argv:
        from core.version import APP_VERSION

        _print(f"Flint {APP_VERSION}")
        return EXIT_OK
    if not argv or argv[0] in ("help", "--help", "-h"):
        _print(_usage())
        return EXIT_OK
    command = argv[0]
    if command not in _COMMANDS:
        _print(f"unknown command: {command}\n{_usage()}")
        return EXIT_USAGE
    opts, error = _opts(argv[1:])
    if error:
        _print(f"RESULT fail: {error}\n{_usage()}")
        return EXIT_USAGE

    if command == "list":
        # Drive discovery needs no privileges; skip the UAC relaunch.
        return _cmd_list(opts)

    elevated = ensure_elevated([sys.executable, *sys.argv])
    if elevated is not None:
        return elevated

    _app = QCoreApplication.instance() or QCoreApplication([])
    try:
        return _COMMANDS[command](opts)
    except Exception as exc:  # never die silently in scripts
        _print(f"RESULT fail: internal error: {exc}")
        return EXIT_FAIL
