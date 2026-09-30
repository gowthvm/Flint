"""Fleet-mode policy tests: capacity gating, per-stick tracking, expiry.

No real drives and no Qt are involved; every behaviour here is pure
policy logic from core.fleet.
"""

from datetime import datetime, timedelta

from core import fleet


def _stamp(days_ago=0, hours_ago=0):
    """History timestamp inside/outside the skip window, dynamically."""
    moment = datetime.now().astimezone() - timedelta(
        days=days_ago, hours=hours_ago
    )
    return moment.isoformat(timespec="seconds")


def _drive(serial="SN123", path=r"\\.\PHYSICALDRIVE1", size_gb=32, **extra):
    return {"serial": serial, "physical_path": path, "size_gb": size_gb, **extra}


def _image(tmp_path, name="ubuntu.iso", size=1_000):
    p = tmp_path / name
    p.write_bytes(b"\x00" * size)
    return str(p)


class TestDriveFingerprint:
    def test_serial_preferred(self):
        assert fleet.drive_fingerprint(_drive()) == "SN123"

    def test_path_fallback_without_serial(self):
        assert (
            fleet.drive_fingerprint(_drive(serial="")) == r"\\.\PHYSICALDRIVE1"
        )

    def test_none_when_neither_known(self):
        assert fleet.drive_fingerprint({"size_gb": 8}) is None


class TestCapacityGate:
    def test_image_fits_with_room(self, tmp_path):
        img = _image(tmp_path, size=2_000)
        session = fleet.FleetSession(images=[img])
        assert session.fits_on_drive(img, _drive(size_gb=8))

    def test_capacity_boundary_is_inclusive(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        # 0.000001 * 1e9 = exactly 1000 bytes of claimed capacity
        session = fleet.FleetSession(images=[img])
        assert session.fits_on_drive(img, _drive(size_gb=0.000001))

    def test_image_larger_than_drive_rejected(self, tmp_path):
        img = _image(tmp_path, size=2_000)
        session = fleet.FleetSession(images=[img])
        assert not session.fits_on_drive(img, _drive(size_gb=0.000001))

    def test_zero_capacity_rejected(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        assert not session.fits_on_drive(img, _drive(size_gb=0))

    def test_missing_image_rejected(self):
        session = fleet.FleetSession(images=[r"C:\nope.iso"])
        assert not session.fits_on_drive(r"C:\nope.iso", _drive())

    def test_drive_without_capacity_rejected(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        assert not session.fits_on_drive(img, {"serial": "SN"})

    def test_exact_size_bytes_beats_rounded_gb(self, tmp_path):
        """A stick whose size_gb rounds below the image must still qualify
        when the exact byte count is available (fleet capacity regression)."""
        img = _image(tmp_path, size=1_500)
        session = fleet.FleetSession(images=[img])
        # size_gb=0.000001 rounds the claimed capacity down to 1000 bytes.
        assert not session.fits_on_drive(img, _drive(size_gb=0.000001))
        assert session.fits_on_drive(
            img, _drive(size_gb=0.000001, size_bytes=2_000)
        )

    def test_size_bytes_rejects_oversized_image(self, tmp_path):
        img = _image(tmp_path, size=2_000)
        session = fleet.FleetSession(images=[img])
        assert not session.fits_on_drive(
            img, _drive(size_gb=0.000001, size_bytes=1_500)
        )

    def test_all_images_must_fit_for_candidate(self, tmp_path):
        small = _image(tmp_path, "a.iso", size=1_000)
        big = _image(tmp_path, "b.iso", size=2_000)
        # capacity is exactly enough for `small` but not both
        session = fleet.FleetSession(images=[small, big])
        drive = _drive(size_gb=0.000001)
        assert not fleet.pick_candidate([drive], session)

    def test_missing_image_blocks_candidate(self, tmp_path):
        good = _image(tmp_path, "a.iso", size=1_000)
        session = fleet.FleetSession(images=[good, r"C:\gone.iso"])
        assert fleet.pick_candidate([_drive()], session) is None


class TestSessionTracking:
    def test_flashed_drive_is_skipped(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        d1 = _drive(serial="SN1")
        d2 = _drive(serial="SN2")
        assert fleet.pick_candidate([d1, d2], session) is d1
        session.mark_flashed(d1)
        assert session.done_count == 1
        assert fleet.pick_candidate([d1, d2], session) is d2

    def test_failed_drive_can_be_retried(self, tmp_path):
        """A failure is recorded but does not blacklist the stick: the
        operator may re-insert it for another attempt."""
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        d = _drive(serial="SN1")
        session.mark_failed()
        assert fleet.pick_candidate([d], session) is d
        assert session.failed_count == 1

    def test_finished_session_sweeps_every_stick_once(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        ds = [_drive(serial=f"SN{i}") for i in range(4)]
        for d in ds:
            assert fleet.pick_candidate(ds, session) is d
            session.mark_flashed(d)
        assert fleet.pick_candidate(ds, session) is None
        assert session.done_count == 4

    def test_sticks_without_serial_tracked_by_path(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        d = _drive(serial="", path=r"\\.\PHYSICALDRIVE9")
        assert fleet.pick_candidate([d], session) is d
        session.mark_flashed(d)
        assert fleet.pick_candidate([d], session) is None


class TestExpiry:
    def test_expired_session_blocks_candidates(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        now = 1_000.0
        session.last_activity = now - fleet.IDLE_EXPIRY_SECONDS - 1
        assert fleet.pick_candidate([_drive()], session, now=now) is None
        assert session.expired(now)

    def test_active_session_keeps_candidates(self, tmp_path):
        img = _image(tmp_path, size=1_000)
        session = fleet.FleetSession(images=[img])
        now = 1_000.0
        session.last_activity = now - 10
        assert fleet.pick_candidate([_drive()], session, now=now) is not None


def test_skip_flashed_drive_skipped(tmp_path, monkeypatch):
    """A drive with a successful flash record is skipped."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": _stamp()}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is None


def test_flashed_drive_shown_when_skip_disabled(tmp_path, monkeypatch):
    """Without skip_flashed, previously-flashed drives are still picked."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": _stamp()}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=False) is d


def test_failed_history_does_not_skip(tmp_path, monkeypatch):
    """A failed flash record does not cause skipping."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": False, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": _stamp()}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_wrong_image_does_not_skip(tmp_path, monkeypatch):
    """History for a different image does not cause skipping."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "fedora.iso", "timestamp": _stamp()}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_empty_history_does_not_skip(tmp_path, monkeypatch):
    """Empty history means nothing to skip."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr("core.history.load_history", list)
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_corrupt_history_does_not_skip(tmp_path, monkeypatch):
    """Corrupt/unreadable history does not block fleet."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr("core.history.load_history", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_stale_flash_record_does_not_skip(tmp_path, monkeypatch):
    """A record older than the skip window is no longer a reason to skip
    the stick: it is back in the rotation."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": _stamp(days_ago=fleet.SKIP_FLASHED_WINDOW_DAYS + 90)}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_record_inside_the_window_still_skips(tmp_path, monkeypatch):
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": _stamp(days_ago=fleet.SKIP_FLASHED_WINDOW_DAYS - 1)}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is None


def test_skip_window_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "SKIP_FLASHED_WINDOW_DAYS", 30)
    img = _image(tmp_path, "ubuntu.iso")
    d = _drive(serial="SN1")

    def _pick(days):
        session = fleet.FleetSession(images=[img])
        monkeypatch.setattr(
            "core.history.load_history",
            lambda: [{"success": True, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": _stamp(days_ago=days)}],
        )
        return fleet.pick_candidate([d], session, skip_flashed=True)

    assert _pick(10) is None  # inside 30 days -> skipped
    assert _pick(45) is d  # outside 30 days -> offered again


def test_serial_less_drive_skipped_by_physical_path(tmp_path, monkeypatch):
    """L07: a stick with no serial falls back to its device path, the same
    way drive_fingerprint does, so the skip toggle can work for it."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": None, "iso": "ubuntu.iso", "timestamp": _stamp(), "physical_path": r"\\.\PHYSICALDRIVE1"}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is None


def test_serial_less_drive_not_skipped_for_another_path(
    tmp_path, monkeypatch
):
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="", path=r"\\.\PHYSICALDRIVE7")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": None, "iso": "ubuntu.iso", "timestamp": _stamp(), "physical_path": r"\\.\PHYSICALDRIVE1"}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_serial_less_drive_not_skipped_without_a_recorded_path(
    tmp_path, monkeypatch
):
    """Older records carry no device path: conservative (may re-flash)
    rather than skipping a stick on an unverifiable identity."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": None, "iso": "ubuntu.iso", "timestamp": _stamp()}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_serial_match_ignores_a_path_for_a_different_stick(
    tmp_path, monkeypatch
):
    """When a serial is present it decides alone: a stale path record for
    another stick must not suppress this one."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1", path=r"\\.\PHYSICALDRIVE7")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN9", "iso": "ubuntu.iso", "timestamp": _stamp(), "physical_path": r"\\.\PHYSICALDRIVE7"}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_recorded_flash_recognises_a_serial_less_stick(
    tmp_path, monkeypatch
):
    """End to end: what flash_report writes, was_recently_flashed reads."""
    from core import history

    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "h.json")
    entry = history.flash_report(
        "ubuntu.iso",
        "Generic Stick",
        1.0,
        True,
        True,
        drive_path=r"\\.\PHYSICALDRIVE1",
    )
    history.save_history([entry])

    assert (
        fleet.was_recently_flashed(
            _drive(serial=""), str(tmp_path / "ubuntu.iso")
        )
        is True
    )
    assert (
        fleet.was_recently_flashed(
            _drive(serial="", path=r"\\.\PHYSICALDRIVE2"),
            str(tmp_path / "ubuntu.iso"),
        )
        is False
    )