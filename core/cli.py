"""Headless / scriptable mode.

Modern invocation (no ``--cli`` prefix needed — it is accepted as a
compat alias)::

    flint list
    flint flash  --image <file> --drive <serial|letter|path> --confirm <serial> [--verify]
    flint verify --drive <serial|letter|path> [--sha256 <hex> --image <file>]
    flint wipe   --drive <serial|letter|path> --confirm <serial> [--method zero|random|nist|dod]
    flint backup --drive <serial|letter|path> --out <file> [--confirm <serial>]
    flint clone  --from <serial|letter|path> --to <serial|letter|path> --confirm <serial of --to>
    flint queue  --file <list.txt> --drive <serial|letter|path> --confirm <serial>
    flint flash-all --image <file> [--image <file> ...] --confirm ARM [--timeout <seconds>]
    flint doctor
    flint completions
    flint help [<command>]

Add ``--json`` anywhere for NDJSON output (:envvar:`FLINT_PROGRESS=json`
is equivalent); :envvar:`FLINT_VERIFY=1` makes ``flash`` verify by
default.

Streams: data and the final ``RESULT`` line go to **stdout**; progress
``FLINT <pct> <speed>MB/s ETA <s>s`` lines and informational notes go
to **stderr**, so scripts can capture stdout as pure data without
``2>&1`` noise.

Every command needing elevation is relaunched via UAC automatically when
run unelevated; ``list``, ``doctor`` and ``completions`` need none.

Destructive commands validate against the live drive list and require
typing the full serial of the destroyed drive (``--confirm``); when run
interactively and no ``--confirm`` is passed, the serial is prompted for
instead.

Exit codes: 0 ok, 1 failure, 2 cancelled, 3 usage/validation, 4
elevation denied.
"""

import ctypes
import json
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
    "timeout",
}
_FLAG_OPTS = {"verify", "json", "help"}

_METHODS = ("zero", "random", "nist", "dod")

_JSON = False


def _print(line: str) -> None:
    """Data line: stdout (RESULT, DRIVE, help, JSON)."""
    try:
        print(line, flush=True)
    except OSError:
        pass


def _eprint(line: str) -> None:
    """Informational line: stderr (progress, notes, usage errors)."""
    try:
        print(line, file=sys.stderr, flush=True)
    except OSError:
        pass


def _emit_json(**fields: object) -> None:
    _print(json.dumps(fields, separators=(",", ":")))


def _result(status: str, message: str, exit_code: int) -> int:
    """Emit the final machine-readable line and return the exit code."""
    if _JSON:
        _emit_json(type="result", status=status, message=message, exit=exit_code)
    else:
        _print(f"RESULT {status}: {message}")
    return exit_code


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in ("1", "true", "yes", "on")


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


def _interactive() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except Exception:
        return False


def _prompt(prompt: str) -> str | None:
    """Read one line from the interactive terminal; None on failure."""
    if not _interactive():
        return None
    try:
        _eprint(prompt)
        line = input()
    except EOFError:
        return None
    except OSError:
        return None
    return line


# ---------------------------------------------------------------------------
# Option parsing and help
# ---------------------------------------------------------------------------


def _opts(argv: list[str]) -> tuple[dict[str, object], str | None]:
    """Parse ``--name value`` pairs; return ``(opts, error)``."""
    opts: dict[str, object] = {}
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
        elif name in _FLAG_OPTS or name == "cli":
            opts[name] = True
            i += 1
        else:
            return {}, f"unknown option: --{name}"
    return opts, None


_COMMAND_HELP: dict[str, str] = {
    "list": (
        "flint list\n"
        "  Print every detected drive with the serial that --confirm expects.\n"
        "  Needs no privileges; drive discovery can be the very first step of\n"
        "  a script (serials are printed exactly as they must be confirmed).\n"
        "  Options: --json\n"
        "  Example: flint list\n"
    ),
    "flash": (
        "flint flash --image <file> --drive <serial|letter|path>\n"
        "           --confirm <serial> [--verify]\n"
        "  Write an image to a drive, erasing everything on it. The image\n"
        "  is written raw (no filesystem) and verified by default when\n"
        "  FLINT_VERIFY=1 or --verify is given.\n"
        "  --image  <file>        the ISO/IMG/DD image to write\n"
        "  --drive  <serial|letter|path>\n"
        "                         target drive (see 'flint list')\n"
        "  --confirm <serial>     full serial of the target drive; when run\n"
        "                         interactively it can be omitted and is\n"
        "                         prompted for instead\n"
        "  --verify               read back the drive and compare digests\n"
        "  Example: flint flash --image C:\\img\\ubuntu.iso --drive E: --confirm 4C530001270509112345\n"
    ),
    "verify": (
        "flint verify --drive <serial|letter|path>\n"
        "             [--sha256 <hex> --image <file>]\n"
        "  Verify a drive. Without --sha256 it is a read-only bad-block scan\n"
        "  of the whole drive. With --sha256 the digest covers only the image\n"
        "  bytes, so --image is required to know how many bytes to compare.\n"
        "  Example: flint verify --drive E: --sha256 0a4b8c… --image C:\\img\\ubuntu.iso\n"
    ),
    "wipe": (
        "flint wipe --drive <serial|letter|path> --confirm <serial>\n"
        "          [--method zero|random|nist|dod]\n"
        "  Erase a drive, destroy all data on it.\n"
        "  --method zero|random|nist|dod   erasure pattern (default zero)\n"
        "  Example: flint wipe --drive E: --confirm 4C530001270509112345 --method nist\n"
    ),
    "backup": (
        "flint backup --drive <serial|letter|path> --out <file>\n"
        "             [--confirm <serial>]\n"
        "  Copy a drive byte-for-byte into an image file. Read-only, so\n"
        "  confirmation is optional.\n"
        "  Example: flint backup --drive E: --out C:\\img\\backup.img\n"
    ),
    "clone": (
        "flint clone --from <serial|letter|path> --to <serial|letter|path>\n"
        "            --confirm <serial of --to>\n"
        "  Copy one drive to another byte-for-byte. Only the target needs\n"
        "  confirmation; it must be at least as large as the source.\n"
        "  Example: flint clone --from E: --to F: --confirm 4C530001270509112346\n"
    ),
    "queue": (
        "flint queue --file <list.txt> --drive <serial|letter|path>\n"
        "            --confirm <serial>\n"
        "  Flash every image listed in a file (one per line; # comments\n"
        "  allowed) to the same drive, stopping on the first failure.\n"
        "  Example: flint queue --file queue.txt --drive E: --confirm 4C530001270509112345\n"
    ),
    "flash-all": (
        "flint flash-all --image <file> [--image <file> ...]\n"
        "                --confirm ARM [--timeout <seconds>]\n"
        "  Fleet mode for scripts: flash every queued image to every drive\n"
        "  that is (or becomes) plugged in, one drive after another, until\n"
        "  the budget expires. A drive is skipped if any image does not fit;\n"
        "  a drive is flashed only once per run; a failed flash aborts the\n"
        "  fleet immediately.\n"
        "  --confirm ARM          arm the fleet; must be the literal word ARM\n"
        "                         (prompted for when run interactively)\n"
        "  --timeout <seconds>    total budget; stop watching after this\n"
        "                         (default 3600); interrupt earlier with Ctrl+C\n"
        "  Example: flint flash-all --image C:\\img\\agent.iso --confirm ARM\n"
    ),
    "doctor": (
        "flint doctor\n"
        "  Print a diagnostic report: version, runtime, Python, elevation,\n"
        "  native-writer availability and the live drive list.\n"
        "  Options: --json\n"
        "  Example: flint doctor\n"
    ),
    "completions": (
        "flint completions\n"
        "  Print a PowerShell completion script (Register-ArgumentCompleter)\n"
        "  for flint. Save it to your $PROFILE to get command, option and\n"
        "  live drive-serial completion.\n"
        "  Example: flint completions | Out-File -Append $PROFILE\n"
    ),
}


def _usage() -> str:
    lines = [
        "Usage: flint <command> [options]",
        "",
        "Commands:",
    ]
    for help_text in _COMMAND_HELP.values():
        first = help_text.splitlines()[0]
        lines.append(f"  {first}")
        lines.append(f"    {help_text.splitlines()[1].strip()}")
    lines.append("")
    lines.append(
        "Run 'flint <command> --help' or 'flint help <command>' for details."
    )
    lines.append("Run 'flint --version' for the version.")
    return "\n".join(lines)


def _command_help(name: str) -> str:
    return _COMMAND_HELP.get(name, _usage())


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------


def ensure_elevated(argv: list[str]) -> int | None:
    """Return None when already elevated (or elevation is unavailable),
    otherwise relaunch elevated and return the child's exit code."""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return None
    except Exception:
        return None
    _eprint("administrator privileges required - relaunching elevated...")
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
        _eprint(f"could not relaunch elevated: {exc}")
        return EXIT_NO_ADMIN
    if proc.returncode != 0:
        _eprint("elevation denied or failed (UAC prompt not accepted?)")
        return EXIT_NO_ADMIN
    try:
        return int((proc.stdout or "").strip() or EXIT_OK)
    except ValueError:
        return EXIT_OK


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------


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


def _confirm_drive(
    drive: dict[str, Any], confirmed: str | None
) -> str | None:
    """Resolve destruction confirmation: --confirm wins; when missing and
    the terminal is interactive, prompt for the full serial instead."""
    if confirmed:
        return _require_confirm(drive, confirmed)
    if _interactive():
        serial = _serial_of(drive)
        name = drive.get("model") or drive.get("name") or "the drive"
        _eprint(
            f"This will ERASE {name} (serial {serial!r}) and every existing "
            "file on it."
        )
        typed = _prompt("Type the full serial to continue:")
        if typed is None:
            return "cancelled (no input)"
        return _require_confirm(drive, typed.strip())
    return "this command destroys a drive: pass --confirm <full serial>"


def _arm_fleet_confirmation(confirmed: str | None) -> str | None:
    """Fleet arming: requires the literal word ARM, prompted when TTY."""
    if confirmed:
        if confirmed.strip().casefold() != "arm":
            return "--confirm must be the literal word ARM"
        return None
    if _interactive():
        typed = _prompt("Type ARM to arm fleet mode (every fitting drive "
                        "will be erased):")
        if typed is None:
            return "cancelled (no input)"
        if typed.strip().casefold() != "arm":
            return "arming cancelled: input was not ARM"
        return None
    return "this destroys every fitting drive: pass --confirm ARM to arm"


# ---------------------------------------------------------------------------
# Worker running
# ---------------------------------------------------------------------------


def _run_worker(worker: Any, label: str) -> tuple[bool, str]:
    """Run a QThread worker to completion, streaming progress.

    Emits ``FLINT <pct> <speed>MB/s ETA <s>s`` on every worker signal and
    at 100% (or one NDJSON progress object per update with ``--json``);
    speed/ETA come from the worker's own signals when it provides them
    (writer/wipe/backup/clone), or are derived from reported bytes, or
    are reported as 0 / remaining-seconds when unavailable (verify).
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
        if _JSON:
            _emit_json(
                type="progress",
                pct=round(state["pct"], 1),
                speed=round(speed, 1),
                eta=eta,
                written=int(state["written"]),
                total=int(state["total"]),
            )
        else:
            _eprint(f"FLINT {state['pct']:.1f} {speed:.1f}MB/s ETA {eta}s")
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

    _eprint(f"hashing {image}...")
    ok, result = compute_sha256(image)
    if not ok:
        _conclude_fail_now(result)
        return None
    _print(f"SHA256 {result}")
    return result


def _conclude_fail_now(message: str) -> None:
    _result("fail", message, EXIT_FAIL)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _drives_json(drives: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [
        {
            "index": index,
            "name": drive.get("name") or drive.get("model") or "unknown device",
            "serial": _serial_of(drive),
            "size_gb": drive.get("size_gb", 0),
            "letters": drive.get("letters")
            or ([drive["letter"]] if drive.get("letter") else []),
            "path": drive.get("physical_path"),
        }
        for index, drive in enumerate(drives, 1)
    ]


def _cmd_list(opts: dict[str, object]) -> int:
    """Print every detected drive with the serial that --confirm expects.

    Needs no privileges, so scripts can discover serials first and then
    run destructive commands with the exact value."""
    drives = _detect_drives()
    if not drives:
        return _result("ok", "no removable drives detected", EXIT_OK)
    if _JSON:
        _emit_json(type="drives", drives=_drives_json(drives))
    else:
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
    return _result("ok", f"{len(drives)} drive(s) listed", EXIT_OK)


def _cmd_flash(opts: dict[str, object]) -> int:
    from core.writer import UsbWriter

    image = str(opts.get("image", ""))
    if not os.path.isfile(image):
        return _result("fail", "--image file not found", EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        _result("fail", "drive not found; detected drives:", EXIT_USAGE)
        for d in drives:
            _print(
                f"  serial={_serial_of(d)!r} "
                f"path={d.get('physical_path')} "
                f"letters={d.get('letters')}"
            )
        return EXIT_USAGE
    issue = _confirm_drive(drive, str(opts.get("confirm", "")))
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    image_size = os.path.getsize(image)
    if image_size > (drive.get("size_gb", 0) * 1_000_000_000):
        return _result("fail", "image is larger than the target drive", EXIT_FAIL)
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = UsbWriter(
        image,
        drive["physical_path"],
        letters=letters,
        verify_after_write=bool(opts.get("verify")) or _env_flag("FLINT_VERIFY"),
    )
    ok, message = _run_worker(worker, "flash")
    if not ok:
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    if not (bool(opts.get("verify")) or _env_flag("FLINT_VERIFY")):
        return _result("ok", "flashed", EXIT_OK)
    digest = _iso_digest(image)
    if digest is None:
        return EXIT_FAIL
    return _cmd_verify_raw(drive["physical_path"], image_size, digest, letters)


def _cmd_verify_raw(
    path: str, size: int, expected: str, letters: list[str]
) -> int:
    from core.verify import VerifyWorker

    worker = VerifyWorker(path, expected, size)
    ok, message = _run_worker(worker, "verify")
    if not ok:
        return _result("fail", message, EXIT_FAIL)
    return _result("ok", "verification passed", EXIT_OK)


def _cmd_verify(opts: dict[str, object]) -> int:
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result("fail", "drive not found", EXIT_USAGE)
    expected = str(opts.get("sha256", ""))
    if expected:
        if len(expected) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in expected
        ):
            return _result("fail", "--sha256 must be a 64-hex digest", EXIT_USAGE)
        # The digest covers only the image bytes, so the drive must be
        # sized to the image (not the whole disk, which is usually larger
        # and would never match). Derive the byte count from --image.
        image = str(opts.get("image", ""))
        if not image:
            return _result(
                "fail",
                "--sha256 requires --image <file> so the byte range to "
                "verify is known",
                EXIT_USAGE,
            )
        if not os.path.isfile(image):
            return _result("fail", "--image file not found", EXIT_USAGE)
        size = os.path.getsize(image)
    else:
        # No digest: read-only bad-block scan of the whole drive.
        from core.verify import verify_device

        _eprint("scanning drive...")
        result = verify_device(drive["physical_path"])
        if not result.get("ok"):
            return _result(
                "fail",
                f"bad sectors: {len(result['bad_sectors'])} ({result['error']})",
                EXIT_FAIL,
            )
        return _result("ok", "no bad sectors", EXIT_OK)
    return _cmd_verify_raw(
        drive["physical_path"], size, expected.lower(),
        drive.get("letters") or [],
    )


def _cmd_wipe(opts: dict[str, object]) -> int:
    from core.wipe import WIPE_METHODS, WipeWorker

    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result("fail", "drive not found", EXIT_USAGE)
    issue = _confirm_drive(drive, str(opts.get("confirm", "")))
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    method = str(opts.get("method", "zero")).lower()
    if method not in WIPE_METHODS:
        return _result(
            "fail", f"--method must be one of {', '.join(WIPE_METHODS)}", EXIT_USAGE
        )
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = WipeWorker(drive["physical_path"], letters, method=method)
    ok, message = _run_worker(worker, "wipe")
    if not ok:
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    return _result("ok", f"wiped ({method})", EXIT_OK)


def _cmd_backup(opts: dict[str, object]) -> int:
    from core.backup import BackupWorker

    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result("fail", "drive not found", EXIT_USAGE)
    out = str(opts.get("out", ""))
    if not out:
        return _result("fail", "--out <file> is required", EXIT_USAGE)
    confirm = str(opts.get("confirm", "")) if opts.get("confirm") else None
    if confirm:
        issue = _require_confirm(drive, confirm)
        if issue:
            return _result("fail", issue, EXIT_USAGE)
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = BackupWorker(drive["physical_path"], out, letters=letters)
    ok, message = _run_worker(worker, "backup")
    if not ok:
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    return _result("ok", f"backup saved to {out}", EXIT_OK)


def _cmd_clone(opts: dict[str, object]) -> int:
    from core.clone import CloneWorker

    drives = _detect_drives()
    source = _resolve_drive(str(opts.get("from", "")), drives)
    target = _resolve_drive(str(opts.get("to", "")), drives)
    if source is None or target is None:
        return _result("fail", "source or target drive not found", EXIT_USAGE)
    if source.get("physical_path") == target.get("physical_path"):
        return _result("fail", "source and target are the same drive", EXIT_USAGE)
    issue = _confirm_drive(target, str(opts.get("confirm", "")))
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    if (target.get("size_gb", 0) * 1_000_000_000) < (
        source.get("size_gb", 0) * 1_000_000_000
    ):
        return _result("fail", "target drive is smaller than the source", EXIT_USAGE)
    worker = CloneWorker(
        source["physical_path"],
        target["physical_path"],
        source_letters=source.get("letters") or [],
        target_letters=target.get("letters") or [],
    )
    ok, message = _run_worker(worker, "clone")
    if not ok:
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    return _result("ok", "clone complete", EXIT_OK)


def _cmd_queue(opts: dict[str, object]) -> int:
    """Flash every image listed in a file (one per line, # comments
    allowed) to the same drive, stopping on the first failure."""
    queue_file = str(opts.get("file", ""))
    if not os.path.isfile(queue_file):
        return _result("fail", "--file not found", EXIT_USAGE)
    images: list[str] = []
    try:
        with open(queue_file, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if not os.path.isfile(line):
                    return _result("fail", f"queue item not found: {line}", EXIT_USAGE)
                images.append(line)
    except OSError as exc:
        return _result("fail", f"could not read queue: {exc}", EXIT_USAGE)
    if not images:
        return _result("fail", "queue file has no images", EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result("fail", "drive not found", EXIT_USAGE)
    issue = _confirm_drive(drive, str(opts.get("confirm", "")))
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    verify = bool(opts.get("verify")) or _env_flag("FLINT_VERIFY")
    for index, image in enumerate(images, 1):
        _eprint(f"--- queue {index}/{len(images)}: {os.path.basename(image)}")
        from core.writer import UsbWriter

        worker = UsbWriter(
            image,
            drive["physical_path"],
            letters=letters,
            verify_after_write=verify,
        )
        ok, message = _run_worker(worker, "flash")
        if not ok:
            return _result(
                "canceled" if message == "cancelled" else "fail",
                f"queue stopped at {image} ({message})",
                EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
            )
    return _result("ok", f"queue complete ({len(images)} images)", EXIT_OK)


def _cmd_flash_all(opts: dict[str, object]) -> int:
    """Fleet mode: flash every queued image to every drive that is (or
    becomes) plugged in until the time budget expires. The session uses
    the same policy as the GUI's fleet mode (core.fleet): skipped drives
    never re-flash, non-fitting drives are passed over, a failed flash
    aborts immediately, Ctrl+C between flashes cancels."""
    from core.fleet import FleetSession, pick_candidate
    from core.writer import UsbWriter

    raw_images = opts.get("images")
    images = [str(i) for i in raw_images] if isinstance(raw_images, list) else []
    if not images:
        return _result(
            "fail",
            "flash-all requires at least one --image <file>",
            EXIT_USAGE,
        )
    for image in images:
        if not os.path.isfile(image):
            return _result("fail", f"--image file not found: {image}", EXIT_USAGE)
    issue = _arm_fleet_confirmation(str(opts.get("confirm", "")) if opts.get("confirm") else None)
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    try:
        budget = int(str(opts.get("timeout", "3600")))
    except ValueError:
        return _result("fail", "--timeout must be a number of seconds", EXIT_USAGE)
    if budget <= 0:
        return _result("fail", "--timeout must be positive", EXIT_USAGE)

    session = FleetSession(images=images)
    verify = _env_flag("FLINT_VERIFY")
    _eprint(
        f"fleet armed: {len(session.images)} image(s); flashing every "
        f"fitting drive until {budget}s pass"
    )
    end = time.monotonic() + budget
    try:
        while time.monotonic() < end:
            drives = _detect_drives()
            drive = pick_candidate(drives, session, now=time.monotonic())
            if drive is None:
                time.sleep(2)
                continue
            name = drive.get("model") or drive.get("name") or "a drive"
            serial = _serial_of(drive)
            _eprint(f"--- flashing {name} (serial={serial!r})")
            letters = drive.get("letters") or (
                [drive["letter"]] if drive.get("letter") else []
            )
            for image in session.images:
                worker = UsbWriter(
                    image,
                    str(drive["physical_path"]),
                    letters=letters,
                    verify_after_write=verify,
                )
                ok, message = _run_worker(worker, "flash")
                if not ok:
                    return _result(
                        "canceled" if message == "cancelled" else "fail",
                        f"fleet stopped at {image} ({message})",
                        EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
                    )
            session.mark_flashed(drive)
            _eprint(
                f"--- {session.done_count} drive(s) flashed, "
                "waiting for more\u2026"
            )
    except KeyboardInterrupt:
        return _result(
            "canceled",
            f"interrupted after {session.done_count} drive(s) flashed",
            EXIT_CANCELLED,
        )
    return _result(
        "ok", f"fleet complete: {session.done_count} drive(s) flashed", EXIT_OK
    )


def _cmd_doctor(opts: dict[str, object]) -> int:
    import platform

    from core.version import APP_VERSION
    from core.writer import _load_native_writer

    drives = _detect_drives()
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    native = "present" if _load_native_writer() is not None else "missing"
    info: dict[str, object] = {
        "version": APP_VERSION,
        "runtime": "frozen exe" if getattr(sys, "frozen", False) else "python",
        "python": platform.python_version(),
        "admin": admin,
        "native_writer": native,
        "drives": len(drives),
    }
    if _JSON:
        _emit_json(
            type="doctor",
            version=info["version"],
            runtime=info["runtime"],
            python=info["python"],
            admin=info["admin"],
            native_writer=info["native_writer"],
            drives=_drives_json(drives),
        )
    else:
        _eprint("flint doctor")
        for key, value in info.items():
            _eprint(f"  {key:<14} {value}")
        for index, drive in enumerate(drives, 1):
            _print(
                f"DRIVE {index} {drive.get('name') or drive.get('model')} "
                f"serial={_serial_of(drive)!r} "
                f"size={drive.get('size_gb', 0)}GB "
                f"letters={','.join(drive.get('letters') or [])}"
            )
    return _result(
        "ok",
        f"doctor report: {len(drives)} drive(s), native writer {native}",
        EXIT_OK,
    )


_POWERSHELL_COMPLETION = """# flint PowerShell completion
# Save to $PROFILE:  flint completions | Out-File -Append $PROFILE
Register-ArgumentCompleter -CommandName flint -Native -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @('list','flash','verify','wipe','backup','clone','queue','flash-all','doctor','completions','help')
    $options  = @('--image','--drive','--confirm','--verify','--out','--file','--method','--from','--to','--sha256','--timeout','--json','--help','--version')
    try {
        $raw = & flint list --json 2>$null
        $drives = @()
        foreach ($line in $raw) {
            $obj = $line | ConvertFrom-Json
            if ($obj.type -eq 'drives') { $drives = $obj.drives }
        }
    } catch { $drives = @() }
    $serialCandidates = @()
    $modelCandidates  = @()
    foreach ($d in $drives) {
        $serialCandidates += $d.serial
        foreach ($l in $d.letters) { $serialCandidates += $l }
    }
    $tokens = $commandAst.CommandElements | ForEach-Object { $_.ToString() } | Select-Object -Skip 1
    if ($tokens.Count -lt 2) {
        ($commands + $options) |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterName', $_)
            }
    } elseif ($tokens[-1] -match '^--') {
        $options |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterName', $_)
            }
    } else {
        ($serialCandidates + $modelCandidates) |
            Where-Object { $_ -like "$wordToComplete*" } |
            Sort-Object -Unique |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
            }
    }
}
"""


def _cmd_completions(opts: dict[str, object]) -> int:
    _print(_POWERSHELL_COMPLETION)
    return EXIT_OK


_COMMANDS = {
    "list": _cmd_list,
    "flash": _cmd_flash,
    "verify": _cmd_verify,
    "wipe": _cmd_wipe,
    "backup": _cmd_backup,
    "clone": _cmd_clone,
    "queue": _cmd_queue,
    "flash-all": _cmd_flash_all,
    "doctor": _cmd_doctor,
    "completions": _cmd_completions,
}

TOP_LEVEL_COMMANDS = frozenset(_COMMANDS) | {"help"}


def main(argv: list[str] | None = None) -> int:
    _ensure_cli_stdio()
    global _JSON
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--cli" in argv:
        argv.remove("--cli")
    if "--version" in argv:
        from core.version import APP_VERSION

        _print(f"Flint {APP_VERSION}")
        return EXIT_OK
    if os.environ.get("FLINT_PROGRESS", "").strip().lower() == "json":
        _JSON = True
    if "--json" in argv:
        _JSON = True
        argv = [arg for arg in argv if arg != "--json"]
    if not argv:
        _print(_usage())
        return EXIT_OK
    first = argv[0]
    if first in ("help", "--help", "-h"):
        if len(argv) >= 2 and argv[1] in _COMMANDS:
            _print(_command_help(argv[1]))
        else:
            _print(_usage())
        return EXIT_OK
    command = first
    if command not in _COMMANDS:
        _eprint(f"unknown command: {command}")
        _eprint(_usage())
        return EXIT_USAGE
    rest = argv[1:]
    if "--help" in rest or "-h" in rest:
        _print(_command_help(command))
        return EXIT_OK

    opts, error = _opts(rest)
    if error:
        _result("fail", error, EXIT_USAGE)
        _eprint(_usage())
        return EXIT_USAGE

    if command in ("list", "doctor", "completions"):
        # No privileges needed: skip the UAC relaunch.
        return _COMMANDS[command](opts)

    if command == "flash-all":
        images = [
            rest[i + 1]
            for i in range(len(rest) - 1)
            if rest[i] == "--image"
        ]
        opts["images"] = images

    elevated = ensure_elevated([sys.executable, *sys.argv])
    if elevated is not None:
        return elevated

    _app = QCoreApplication.instance() or QCoreApplication([])
    try:
        return _COMMANDS[command](opts)
    except KeyboardInterrupt:
        return _result("canceled", "interrupted by user", EXIT_CANCELLED)
    except Exception as exc:  # never die silently in scripts
        return _result("fail", f"internal error: {exc}", EXIT_FAIL)