"""Fleet-mode policy tests: capacity gating, per-stick tracking, expiry.

No real drives and no Qt are involved; every behaviour here is pure
policy logic from core.fleet.
"""

from core import fleet


def _drive(serial="SN123", path=r"\\.\PHYSICALDRIVE1", size_gb=32):
    return {"serial": serial, "physical_path": path, "size_gb": size_gb}


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
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": "2026-01-01T12:00:00+00:00"}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is None


def test_flashed_drive_shown_when_skip_disabled(tmp_path, monkeypatch):
    """Without skip_flashed, previously-flashed drives are still picked."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": "2026-01-01T12:00:00+00:00"}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=False) is d


def test_failed_history_does_not_skip(tmp_path, monkeypatch):
    """A failed flash record does not cause skipping."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": False, "drive_serial": "SN1", "iso": "ubuntu.iso", "timestamp": "2026-01-01T12:00:00+00:00"}],
    )
    assert fleet.pick_candidate([d], session, skip_flashed=True) is d


def test_wrong_image_does_not_skip(tmp_path, monkeypatch):
    """History for a different image does not cause skipping."""
    img = _image(tmp_path, "ubuntu.iso")
    session = fleet.FleetSession(images=[img])
    d = _drive(serial="SN1")
    monkeypatch.setattr(
        "core.history.load_history",
        lambda: [{"success": True, "drive_serial": "SN1", "iso": "fedora.iso", "timestamp": "2026-01-01T12:00:00+00:00"}],
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