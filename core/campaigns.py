"""Bounded, per-target deployment campaign orchestration."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

from core.jobs import JobManifest, save_manifest

TERMINAL_STATES = frozenset({"passed", "failed", "cancelled"})


@dataclass
class CampaignJob:
    """One target in a campaign and the file that persists its state."""

    manifest: JobManifest
    manifest_path: str
    cancel_event: threading.Event | None = field(default=None, repr=False, compare=False)


@dataclass
class Campaign:
    """A group of independently tracked deployment jobs."""

    jobs: list[CampaignJob]
    max_workers: int = 2
    campaign_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(
        default_factory=lambda: datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
    )

    def __post_init__(self) -> None:
        if not self.jobs:
            raise ValueError("campaign must contain at least one job")
        if self.max_workers < 1:
            raise ValueError("campaign concurrency must be at least one")


class CampaignRunner:
    """Run campaign jobs with bounded concurrency and failure isolation."""

    def __init__(
        self,
        campaign: Campaign,
        worker: Callable[[CampaignJob], None],
        *,
        on_state: Callable[[CampaignJob], None] | None = None,
    ) -> None:
        self.campaign = campaign
        self.worker = worker
        self.on_state = on_state
        self._cancel = threading.Event()
        self._stop_queued = threading.Event()
        self._lock = threading.Lock()
        for job in campaign.jobs:
            job.cancel_event = self._cancel

    def cancel(self) -> None:
        """Cancel queued work and request cancellation from active workers."""
        self._stop_queued.set()
        self._cancel.set()

    def stop_queued(self) -> None:
        """Stop scheduling new jobs while allowing active jobs to finish."""
        self._stop_queued.set()

    @property
    def cancellation_requested(self) -> bool:
        return self._cancel.is_set()

    def _publish(self, job: CampaignJob) -> None:
        with self._lock:
            save_manifest(job.manifest_path, job.manifest)
            if self.on_state is not None:
                self.on_state(job)

    def _run_one(self, job: CampaignJob) -> None:
        if self._stop_queued.is_set():
            job.manifest.state = "cancelled"
            job.manifest.error = "campaign stopped before job started"
            self._publish(job)
            return
        job.manifest.state = "writing"
        job.manifest.error = None
        self._publish(job)
        try:
            self.worker(job)
        except Exception as exc:
            job.manifest.state = "failed"
            job.manifest.error = str(exc)
        else:
            job.manifest.state = (
                "cancelled" if self._cancel.is_set() else "passed"
            )
        self._publish(job)

    def run(self) -> dict[str, int]:
        """Run all jobs and return counts by terminal state."""
        with ThreadPoolExecutor(max_workers=self.campaign.max_workers) as pool:
            futures: list[Future[None]] = [
                pool.submit(self._run_one, job) for job in self.campaign.jobs
            ]
            for future in as_completed(futures):
                future.result()
        counts = {state: 0 for state in TERMINAL_STATES}
        for job in self.campaign.jobs:
            if job.manifest.state in counts:
                counts[job.manifest.state] += 1
        return counts