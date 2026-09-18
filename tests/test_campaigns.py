import threading
import time

import pytest

from core.campaigns import Campaign, CampaignJob, CampaignRunner
from core.jobs import JobManifest, load_manifest


def _job(tmp_path, name):
    manifest = JobManifest(
        source_path=f"{name}.iso",
        source_size=100,
        source_sha256="a" * 64,
        target_fingerprint=f"serial:{name}",
        target_size=1_000,
    )
    return CampaignJob(manifest, str(tmp_path / f"{name}.json"))


def test_campaign_is_bounded_and_isolates_failures(tmp_path):
    jobs = [_job(tmp_path, str(i)) for i in range(4)]
    active = 0
    maximum = 0
    lock = threading.Lock()

    def worker(job):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        if job.manifest.target_fingerprint == "serial:1":
            raise RuntimeError("bad target")

    counts = CampaignRunner(
        Campaign(jobs, max_workers=2), worker
    ).run()

    assert maximum <= 2
    assert counts == {"passed": 3, "failed": 1, "cancelled": 0}
    assert jobs[1].manifest.error == "bad target"
    assert load_manifest(jobs[1].manifest_path).state == "failed"


def test_stop_queued_cancels_jobs_not_started(tmp_path):
    jobs = [_job(tmp_path, str(i)) for i in range(3)]
    started = threading.Event()

    def worker(job):
        started.set()
        time.sleep(0.05)

    runner = CampaignRunner(Campaign(jobs, max_workers=1), worker)

    def stop_after_first():
        started.wait(timeout=1)
        runner.stop_queued()

    stopper = threading.Thread(target=stop_after_first)
    stopper.start()
    counts = runner.run()
    stopper.join()

    assert counts["passed"] == 1
    assert counts["cancelled"] == 2


def test_campaign_rejects_invalid_concurrency(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        Campaign([_job(tmp_path, "one")], max_workers=0)


def test_campaign_job_receives_shared_cancellation_event(tmp_path):
    job = _job(tmp_path, "one")
    observed = []

    def worker(running_job):
        observed.append(running_job.cancel_event is not None)
        running_job.cancel_event.set()

    counts = CampaignRunner(Campaign([job]), worker).run()

    assert observed == [True]
    assert counts["cancelled"] == 1