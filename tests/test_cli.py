"""CLI headless mode: argument parsing, validation and command dispatch
(real drives and workers are never touched; both are faked)."""

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
    assert error == "missing value for --image"


def test_opts_unknown_option():
    _, error = cli._opts(["--explode"])
    assert error == "unknown option: --explode"


def test_main_unknown_command(capsys):
    assert cli.main(["frobnicate"]) == cli.EXIT_USAGE
    assert "unknown command: frobnicate" in capsys.readouterr().out


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
    assert "no images" in capsys.readouterr().out


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
    assert cli.main(["--cli", "--help"]) == cli.EXIT_OK
    assert "Usage: flint --cli" in capsys.readouterr().out


def test_help_command_exits_ok(capsys):
    assert cli.main(["--cli", "help"]) == cli.EXIT_OK
    assert "Usage: flint --cli" in capsys.readouterr().out