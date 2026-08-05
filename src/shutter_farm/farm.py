"""The sweep. Discover, decide, dispatch, record, repeat.

One sweep is the unit of work: walk the root, ask the ledger about each
folder, run the ones that need it, and persist after every job rather than
at the end. Persisting per job is the difference between a container that
can be killed at any moment and one that loses an hour of work when the
node is preempted, which for a batch job on spot capacity is a Tuesday.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from shutter_farm.discovery import Job, discover
from shutter_farm.observability import METRICS, log
from shutter_farm.runner import (
    RunOutcome,
    ToolMissing,
    outputs_of,
    run_job,
)
from shutter_farm.state import Ledger


@dataclass
class SweepResult:
    discovered: int = 0
    ran: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "ran": self.ran,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "duration_seconds": round(self.duration, 2),
        }


class Farm:
    def __init__(
        self,
        root: Path,
        ledger: Ledger,
        *,
        write: bool = False,
        timeout: float = 3600.0,
        max_jobs: int = 0,
        run_fn=None,
    ) -> None:
        self.root = root
        self.ledger = ledger
        self.write = write
        self.timeout = timeout
        self.max_jobs = max_jobs
        # Resolved at call time rather than bound as a default argument, so
        # the dispatcher stays substitutable: tests swap it, and so could a
        # deployment that wanted to run tools some other way.
        self._run_fn = run_fn
        self._stopping = False
        self.last_sweep_at: float = 0.0

    def request_stop(self, *_args) -> None:
        """SIGTERM handler. Finish the current job, then stop cleanly.

        Kubernetes sends SIGTERM and waits out terminationGracePeriod. A
        batch runner that dies mid-job leaves the ledger honest anyway,
        because state is written per job, but finishing the current one is
        free and avoids a wasted restart.
        """
        if not self._stopping:
            log("shutdown_requested", note="finishing the current job, then exiting")
        self._stopping = True

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def sweep(self) -> SweepResult:
        started = time.monotonic()
        result = SweepResult()
        jobs = discover(self.root)
        result.discovered = len(jobs)
        log("sweep_started", root=str(self.root), discovered=len(jobs),
            write_enabled=self.write)

        for job in jobs:
            if self._stopping:
                log("sweep_interrupted", remaining=len(jobs) - result.ran - result.skipped)
                break
            if self.max_jobs and result.ran >= self.max_jobs:
                log("job_cap_reached", cap=self.max_jobs,
                    note="remaining folders stay queued for the next sweep")
                break

            now = time.time()
            should, why = self.ledger.should_run(job, now)
            if not should:
                result.skipped += 1
                log("job_skipped", level="debug", folder=str(job.folder), reason=why)
                continue
            self._run_one(job, why, result)

        self.ledger.save()
        result.duration = time.monotonic() - started
        self.last_sweep_at = time.time()
        self._publish(result)
        log("sweep_finished", **result.as_dict())
        for path, record in self.ledger.quarantined():
            log("folder_quarantined", level="warn", folder=path,
                attempts=record.attempts, error=record.last_error)
        return result

    def _run_one(self, job: Job, why: str, result: SweepResult) -> None:
        log("job_started", folder=str(job.folder), tool=job.tool,
            kind=job.kind, media_files=job.media_count, reason=why)
        dispatch = self._run_fn or run_job
        try:
            outcome: RunOutcome = dispatch(
                job, write=self.write, timeout=self.timeout
            )
        except ToolMissing as exc:
            result.failed += 1
            result.ran += 1
            self.ledger.record_failure(job, error=str(exc), now=time.time())
            self.ledger.save()
            METRICS.inc("shutter_farm_jobs_total", tool=job.tool, result="tool_missing")
            log("job_failed", level="error", folder=str(job.folder),
                tool=job.tool, error=str(exc))
            return

        result.ran += 1
        METRICS.inc("shutter_farm_job_duration_seconds_total",
                    outcome.duration, tool=job.tool)

        if outcome.ok:
            result.succeeded += 1
            self.ledger.record_success(
                job, duration=outcome.duration, now=time.time(),
                outputs=outputs_of(job),
            )
            METRICS.inc("shutter_farm_jobs_total", tool=job.tool, result="success")
            METRICS.inc("shutter_farm_media_files_total", job.media_count, kind=job.kind)
            log("job_finished", folder=str(job.folder), tool=job.tool,
                duration_seconds=round(outcome.duration, 2),
                media_files=job.media_count)
        else:
            result.failed += 1
            record = self.ledger.record_failure(
                job, error=outcome.error, now=time.time()
            )
            result.errors.append(f"{job.folder}: {outcome.error}")
            METRICS.inc("shutter_farm_jobs_total", tool=job.tool,
                        result="timeout" if outcome.timed_out else "failure")
            log("job_failed", level="error", folder=str(job.folder),
                tool=job.tool, exit_code=outcome.exit_code,
                timed_out=outcome.timed_out, attempts=record.attempts,
                status=record.status, error=outcome.error)
        # Persist after every job: a preempted node must not lose the batch.
        self.ledger.save()

    def _publish(self, result: SweepResult) -> None:
        for status, count in self.ledger.counts().items():
            METRICS.set("shutter_farm_folders", count, status=status)
        METRICS.set("shutter_farm_last_run_timestamp_seconds", time.time())
