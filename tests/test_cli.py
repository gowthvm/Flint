"""CLI headless mode: argument parsing, validation and command dispatch
(real drives and workers are never touched; both are faked). Data and
``RESULT`` lines go to stdout, progress/notes go to stderr."""

import json
import os

import pytest

from core import cli


def _fake_drives():
    return [
        {
            "physical_path": r"\\.\PHYSICALDRIVE3",
            "serial": "ABC1234",
            "model": "USB Stick",
            "size_gb": 16,
            "letter": "E",
            "letters": ["E"],
            "bus_type": "USB",
            "name": "USB Stick",
        }
    ]


@pytest.fixture(autouse=True)
def _no_elevation(monkeypatch):
    monkeypatch.setattr(cli, "ensure_elevated", lambda argv: None)


@pytest.fixture(autouse=True)
def _reset_json():
    """main() sets the module-global _JSON flag; each test starts clean."""
    cli._JSON = False
    yield
    cli._JSON = False


def test_opts_parsing():
    opts, error = cli._opts(
        ["--image", "a.iso", "--drive", "E", "--confirm", "ABC1234", "--verify"]
    )
    assert error is None
    assert opts == {
        "image": "a.iso",
        "drive": "E",
        "confirm": "ABC1234",
        "verify": True,
    }


def test_opts_missing_value():
    _, error = cli._opts(["--image"])
    assert error == "requires --image <file>"


def test_opts_unknown_option():
    _, error = cli._opts(["--explode"])
    assert error == "unknown option: --explode"


def test_main_unknown_command(capsys):
    assert cli.main(["frobnicate"]) == cli.EXIT_USAGE
    assert "unknown command: frobnicate" in capsys.readouterr().err


def test_flash_rejects_missing_image_file(tmp_path):
    rc = cli._cmd_flash({"image": str(tmp_path / "nope.iso"), "drive": "E"})
    assert rc == cli.EXIT_USAGE


def test_flash_drive_not_found(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", list)
    rc = cli._cmd_flash({"image": str(image), "drive": "E"})
    assert rc == cli.EXIT_USAGE
    assert "drive not found" in capsys.readouterr().out


def test_flash_confirmation_must_match_serial(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_flash({"image": str(image), "drive": "E", "confirm": "WRONG"})
    assert rc == cli.EXIT_USAGE
    assert "does not match the drive serial" in capsys.readouterr().out


def test_flash_happy_path(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker, label: (True, ""))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_flash_failure_exit_code(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker, label: (False, "boom"))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_FAIL
    assert "RESULT fail: boom" in capsys.readouterr().out


def test_wipe_unknown_method_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_wipe(
        {
            "drive": "E",
            "confirm": "ABC1234",
            "method": "flames",
        }
    )
    assert rc == cli.EXIT_USAGE
    assert "must be one of" in capsys.readouterr().out


def test_wipe_valid_method_dispatches(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker, label: (True, ""))
    worker_used = []

    def _capture_worker(worker, label):
        worker_used.append(worker.method)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _capture_worker)
    rc = cli._cmd_wipe(
        {"drive": "E", "confirm": "ABC1234", "method": "dod"}
    )
    assert rc == cli.EXIT_OK
    assert worker_used == ["dod"]


def test_backup_requires_out(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_backup({"drive": "E", "confirm": "ABC1234"})
    assert rc == cli.EXIT_USAGE
    assert "--out" in capsys.readouterr().out


def test_clone_rejects_same_drive(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_clone(
        {"from": "E", "to": r"\\.\PHYSICALDRIVE3", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_USAGE
    assert "same drive" in capsys.readouterr().out


def test_clone_requires_target_confirmation(tmp_path, monkeypatch, capsys):
    two_drives = [dict(d) for d in _fake_drives()]
    second = dict(two_drives[0])
    second["physical_path"] = r"\\.\PHYSICALDRIVE4"
    second["serial"] = "ZZZ999"
    second["letter"] = "F"
    second["letters"] = ["F"]
    two_drives.append(second)
    monkeypatch.setattr(cli, "_detect_drives", lambda: two_drives)

    rc = cli._cmd_clone({"from": "E", "to": "F", "confirm": "NOPE"})
    assert rc == cli.EXIT_USAGE
    assert "does not match the drive serial" in capsys.readouterr().out


def test_queue_missing_file():
    assert cli._cmd_queue({"file": "does-not-exist.txt"}) == cli.EXIT_USAGE


def test_queue_empty_file(tmp_path, capsys):
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text("# nothing here\n\n")
    rc = cli._cmd_queue({"file": str(queue_file)})
    assert rc == cli.EXIT_USAGE
    assert "no valid images" in capsys.readouterr().out


def test_queue_happy_path_with_quoted_paths(tmp_path, monkeypatch, capsys):
    first = tmp_path / "one.iso"
    second = tmp_path / "two.iso"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text(f'"{first}"\n"{second}"\n')
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker, label):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    rc = cli._cmd_queue(
        {"file": str(queue_file), "drive": "E", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_OK
    assert started == [str(first), str(second)]


def test_queue_happy_path_with_relative_paths(tmp_path, monkeypatch, capsys):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text("a.iso\n")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker, label):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    rc = cli._cmd_queue(
        {"file": str(queue_file), "drive": "E", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_OK
    assert started == [str(img)]


def test_queue_flashes_every_image_then_reports_count(
    tmp_path, monkeypatch, capsys
):
    first = tmp_path / "one.iso"
    second = tmp_path / "two.iso"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text(f"{first}\n\n# comment\n{second}\n")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker, label):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    rc = cli._cmd_queue(
        {"file": str(queue_file), "drive": "E", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_OK
    assert started == [str(first), str(second)]
    assert "queue complete (2 images)" in capsys.readouterr().out


def test_main_dispatches_flash_command(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker, label: (True, ""))

    rc = cli.main(
        [
            "--cli",
            "flash",
            "--image",
            str(image),
            "--drive",
            "E",
            "--confirm",
            "ABC1234",
            "--verify",
        ]
    )

    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "RESULT ok" in out


def test_help_prints_usage(capsys):
    assert cli.main(["--help"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Usage: flint <command>" in out
    assert "list" in out
    assert "nist" in out


def test_help_command_exits_ok(capsys):
    assert cli.main(["help"]) == cli.EXIT_OK
    assert "Usage: flint <command>" in capsys.readouterr().out


def test_version_flag(capsys):
    from core.version import APP_VERSION

    assert cli.main(["--cli", "--version"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert f"Flint  v{APP_VERSION}" in captured.err


def test_list_prints_drives_with_serials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli.main(["--cli", "list"])
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "ABC1234" in captured.err
    assert "RESULT ok: 1 drive(s) listed" in captured.out


def test_list_no_drives(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", list)
    assert cli.main(["--cli", "list"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "no removable drives" in captured.out


def test_list_does_not_relaunch_elevated(monkeypatch, capsys):
    """list needs no privileges, so it must skip the UAC relaunch."""
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    called = []

    def _ensure_elevated(argv):
        called.append(argv)
        raise AssertionError("list must not relaunch elevated")

    monkeypatch.setattr(cli, "ensure_elevated", _ensure_elevated)
    rc = cli.main(["--cli", "list"])
    assert rc == cli.EXIT_OK
    assert called == []
    capsys.readouterr()


def test_run_worker_emits_flint_progress_lines(monkeypatch, capsys):
    """The documented machine format is FLINT <pct> <speed>MB/s ETA <s>s."""
    from PyQt6.QtCore import QCoreApplication, QThread, pyqtSignal

    class _FakeWorker(QThread):
        progress = pyqtSignal(float)
        speed_mbps = pyqtSignal(float)
        written_bytes = pyqtSignal(int)
        total_bytes = pyqtSignal(int)
        finished = pyqtSignal(bool, str)

        def run(self) -> None:
            self.total_bytes.emit(1_000_000_000)
            self.progress.emit(10.0)
            self.progress.emit(50.0)
            self.speed_mbps.emit(42.5)
            self.written_bytes.emit(500_000_000)
            self.progress.emit(100.0)
            self.finished.emit(True, "ok")

    app = QCoreApplication([])
    worker = _FakeWorker()
    ok, message = cli._run_worker(worker, "test")
    assert QCoreApplication.instance() is app
    assert ok and message == "ok"
    err = capsys.readouterr().err
    assert "FLINT" in err and "42.5MB/s ETA 0s" in err


def test_verify_sha256_requires_image(monkeypatch, capsys):
    """The digest covers only image bytes, so --image must be given to
    know how many bytes to compare (whole-drive comparison of a larger
    disk would never match the image digest)."""
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    rc = cli.main(
        ["--cli", "verify", "--drive", "E", "--sha256", "a" * 64]
    )
    assert rc == cli.EXIT_USAGE
    out = capsys.readouterr().out
    assert "--image" in out


def test_verify_sha256_missing_image_file(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    rc = cli.main(
        [
            "--cli",
            "verify",
            "--drive",
            "E",
            "--sha256",
            "a" * 64,
            "--image",
            "nope.iso",
        ]
    )
    assert rc == cli.EXIT_USAGE
    assert "not found" in capsys.readouterr().out


def test_verify_sha256_derives_size_from_image(tmp_path, monkeypatch):
    image = tmp_path / "img.iso"
    image.write_bytes(os.urandom(4096))
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    calls: list[tuple] = []

    def _fake_verify_raw(path, size, expected, letters):
        calls.append((path, size, expected, letters))
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_cmd_verify_raw", _fake_verify_raw)
    rc = cli.main(
        [
            "--cli",
            "verify",
            "--drive",
            "E",
            "--sha256",
            "a" * 64,
            "--image",
            str(image),
        ]
    )
    assert rc == cli.EXIT_OK
    assert calls == [(r"\\.\PHYSICALDRIVE3", 4096, "a" * 64, ["E"])]


# --- top-level dispatch and help -----------------------------------------


def test_no_args_prints_usage(capsys):
    assert cli.main([]) == cli.EXIT_OK
    assert "Usage: flint <command>" in capsys.readouterr().out


def test_flash_has_own_help(capsys):
    assert cli.main(["flash", "--help"]) == cli.EXIT_OK
    assert "flint flash --image" in capsys.readouterr().out


def test_flash_help_short_flag(capsys):
    assert cli.main(["flash", "-h"]) == cli.EXIT_OK
    assert "flint flash --image" in capsys.readouterr().out


def test_help_for_specific_command(capsys):
    assert cli.main(["help", "flash-all"]) == cli.EXIT_OK
    assert "flash-all" in capsys.readouterr().out


def test_help_unknown_target_falls_back_to_usage(capsys):
    assert cli.main(["help", "bogus"]) == cli.EXIT_OK
    assert "Usage: flint <command>" in capsys.readouterr().out


def test_unexpected_positional_argument_rejected(capsys):
    assert cli.main(["flash", "extra"]) == cli.EXIT_USAGE
    assert "unexpected argument" in capsys.readouterr().out


# --- JSON (NDJSON) -------------------------------------------------------


def test_list_json_shape(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli.main(["list", "--json"]) == cli.EXIT_OK
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    kinds = [obj["type"] for obj in objects]
    assert "drives" in kinds and "result" in kinds
    drives = next(o for o in objects if o["type"] == "drives")["drives"]
    assert drives[0]["serial"] == "ABC1234"
    assert drives[0]["letters"] == ["E"]
    assert drives[0]["path"] == r"\\.\PHYSICALDRIVE3"


def test_flint_progress_json_env_equivalent(monkeypatch, capsys):
    monkeypatch.setenv("FLINT_PROGRESS", "json")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli.main(["list"]) == cli.EXIT_OK
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all("type" in obj for obj in objects)
    assert any(
        obj["type"] == "result" and obj["status"] == "ok" for obj in objects
    )


def test_result_json_object(monkeypatch, capsys, tmp_path):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker, label: (False, "boom"))
    monkeypatch.setattr(cli, "_JSON", True)
    assert cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    ) == cli.EXIT_FAIL
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert objects == [
        {
            "type": "result",
            "status": "fail",
            "message": "boom",
            "exit": cli.EXIT_FAIL,
        }
    ]


def test_json_result_uses_exit_field_for_status(capsys):
    assert cli.main(["backup", "--json", "--drive", "E"]) == cli.EXIT_USAGE
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert objects[-1]["type"] == "result"
    assert objects[-1]["status"] == "fail"
    assert objects[-1]["exit"] == cli.EXIT_USAGE


# --- confirmations -------------------------------------------------------


def test_flash_prompts_for_serial_when_tty(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker, label: (True, ""))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "ABC1234")

    assert cli._cmd_flash({"image": str(image), "drive": "E"}) == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_flash_prompt_wrong_serial_rejected(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "WRONG")

    assert cli._cmd_flash({"image": str(image), "drive": "E"}) == cli.EXIT_USAGE
    assert "does not match the drive serial" in capsys.readouterr().out


def test_flash_confirm_required_when_not_tty(tmp_path, monkeypatch):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    assert cli._cmd_flash({"image": str(image), "drive": "E"}) == cli.EXIT_USAGE


# --- flash-all (fleet) ---------------------------------------------------


def test_flash_all_requires_images(capsys):
    assert cli._cmd_flash_all({"confirm": "ARM"}) == cli.EXIT_USAGE
    assert "--image" in capsys.readouterr().out


def test_flash_all_requires_arm_when_not_interactive(tmp_path, monkeypatch):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    assert cli._cmd_flash_all({"images": [str(image)]}) == cli.EXIT_USAGE


def test_flash_all_rejects_other_confirm_words(tmp_path, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    assert (
        cli._cmd_flash_all({"images": [str(image)], "confirm": "yes"})
        == cli.EXIT_USAGE
    )
    assert "literal word ARM" in capsys.readouterr().out


def test_flash_all_missing_image_file(tmp_path, capsys):
    assert (
        cli._cmd_flash_all(
            {"images": [str(tmp_path / "nope.iso")], "confirm": "ARM"}
        )
        == cli.EXIT_USAGE
    )
    assert "file not found" in capsys.readouterr().out


def test_flash_all_timeout_validation(tmp_path):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    base = {"images": [str(image)], "confirm": "ARM"}
    assert cli._cmd_flash_all({**base, "timeout": "abc"}) == cli.EXIT_USAGE
    assert cli._cmd_flash_all({**base, "timeout": "0"}) == cli.EXIT_USAGE


def test_flash_all_flashes_every_image_to_every_drive(
    tmp_path, monkeypatch, capsys
):
    one = tmp_path / "one.iso"
    two = tmp_path / "two.iso"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker, label):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli.main(
        [
            "flash-all",
            "--image",
            str(one),
            "--image",
            str(two),
            "--confirm",
            "ARM",
            "--timeout",
            "1",
        ]
    )

    assert rc == cli.EXIT_OK
    assert started == [str(one), str(two)]
    assert "1 drive(s) flashed" in capsys.readouterr().out


def test_flash_all_interrupt_cancels(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    def _interrupt(worker, label):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_run_worker", _interrupt)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli._cmd_flash_all(
        {"images": [str(image)], "confirm": "ARM", "timeout": "30"}
    )

    assert rc == cli.EXIT_CANCELLED
    assert "interrupted after 0 drive(s) flashed" in capsys.readouterr().out


# --- verify --------------------------------------------------------------


def test_verify_rejects_bad_hex(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert (
        cli.main(["verify", "--drive", "E", "--sha256", "z" * 64])
        == cli.EXIT_USAGE
    )
    assert "64-hex digest" in capsys.readouterr().out


def test_verify_bad_block_scan_ok(monkeypatch, capsys):
    from core import verify as verify_mod

    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(
        verify_mod,
        "verify_device",
        lambda path: {"ok": True, "bad_sectors": []},
    )
    assert cli.main(["verify", "--drive", "E"]) == cli.EXIT_OK
    assert "no bad sectors" in capsys.readouterr().out


def test_verify_bad_block_scan_fails(monkeypatch, capsys):
    from core import verify as verify_mod

    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(
        verify_mod,
        "verify_device",
        lambda path: {"ok": False, "bad_sectors": [1, 2], "error": "boom"},
    )
    assert cli.main(["verify", "--drive", "E"]) == cli.EXIT_FAIL
    assert "bad sectors: 2 (boom)" in capsys.readouterr().out


# --- doctor and completions ----------------------------------------------


def test_doctor_reports_and_skips_elevation(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    called = []

    def _never(argv):
        called.append(argv)
        raise AssertionError("doctor must not relaunch elevated")

    monkeypatch.setattr(cli, "ensure_elevated", _never)
    assert cli.main(["doctor"]) == cli.EXIT_OK
    assert called == []
    captured = capsys.readouterr()
    assert "doctor report: 1 drive(s)" in captured.out
    assert "Flint Doctor" in captured.err
    assert "ABC1234" in captured.out


def test_doctor_json_shape(monkeypatch, capsys):
    from core.version import APP_VERSION

    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli.main(["doctor", "--json"]) == cli.EXIT_OK
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    report = next(o for o in objects if o["type"] == "doctor")
    assert report["version"] == APP_VERSION
    assert len(report["drives"]) == 1
    assert report["drives"][0]["serial"] == "ABC1234"
    assert any(o["type"] == "result" for o in objects)


def test_completions_prints_powershell_script(capsys):
    assert cli.main(["completions"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Register-ArgumentCompleter" in out
    assert "flash-all" in out


# --- streams -------------------------------------------------------------


def test_flash_progress_lines_never_pollute_stdout(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker, label: (True, ""))
    assert cli.main(["flash", "--image", str(image), "--drive", "E",
                     "--confirm", "ABC1234"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "FLINT " not in captured.out
    for line in captured.out.splitlines():
        assert "RESULT" in line


def test_scan_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import verify as verify_mod
    monkeypatch.setattr(
        verify_mod,
        "whole_drive_scan",
        lambda *a, **kw: {"ok": True, "bad_sectors": [], "digest": "ab", "speed_mbps": 1.0, "drive_size": 1000, "error": ""},
    )
    rc = cli.main(["--cli", "scan", "--drive", "E"])
    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_scan_reports_bad_sectors(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import verify as verify_mod
    monkeypatch.setattr(
        verify_mod,
        "whole_drive_scan",
        lambda *a, **kw: {"ok": False, "bad_sectors": [{"offset": 0, "length": 4096}] * 3, "digest": "", "speed_mbps": 1.0, "drive_size": 1000, "error": ""},
    )
    rc = cli.main(["--cli", "scan", "--drive", "E"])
    assert rc == cli.EXIT_FAIL
    assert "RESULT fail" in capsys.readouterr().out


def test_scan_drive_not_found(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", list)
    rc = cli._cmd_scan({"drive": "NOPE"})
    assert rc == cli.EXIT_USAGE
    assert "drive not found" in capsys.readouterr().out