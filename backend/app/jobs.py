"""Run bounded Agent executions and give the API one job interface in either storage mode."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import time
from typing import Protocol
from uuid import uuid4

from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, RateLimitError

from backend.app.agent import AgentRun
from backend.app.checkpoint import AgentCheckpoint, CheckpointWriter
from backend.app.config import Settings
from backend.app.github import REPOSITORY_CHECK_FAILED, REPOSITORY_NOT_PUBLIC
from backend.app.models import (
    AnalysisResult, EventKind, IssueTarget, JobSnapshot, JobStatus, ProgressEvent, TERMINAL_STATUSES,
)
from backend.app.storage import POLL_SECONDS, PostgresAnalysisStore, StorageError
from backend.app.trace import AnalysisTrace, TraceLog


logger = logging.getLogger(__name__)
QUEUE_FULL = "The local queue is full. Wait for an analysis to finish."
ACCOUNT_LIMIT = "This account has reached its limit of analyses in progress ({limit}). Wait for one to finish before starting another."


class QueueFullError(Exception):
    """The local job store has reached its capacity."""


class AccountLimitError(QueueFullError):
    """One signed-in account holds its full share of the shared queue."""


@dataclass
class Job:
    snapshot: JobSnapshot
    created: float = field(default_factory=time.monotonic)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    suspended: bool = False
    cancel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    trace: TraceLog = field(default_factory=TraceLog)

    def emit(self, kind: EventKind, message: str) -> None:
        self.snapshot.events.append(ProgressEvent(
            id=len(self.snapshot.events) + 1,
            kind=kind,
            message=message,
        ))
        self.changed.set()


def queued_snapshot(target: IssueTarget) -> JobSnapshot:
    return JobSnapshot(
        job_id=uuid4().hex, issue_url=target.url, status="queued", created_at=datetime.now(UTC),
        events=[ProgressEvent(id=1, kind="status", message="Analysis queued.")],
    )


def has_update(snapshot: JobSnapshot, after: int) -> bool:
    """Whether a reader that has seen events up to `after` has something new to read."""
    return snapshot.status in TERMINAL_STATUSES or bool(snapshot.events and snapshot.events[-1].id > after)


class JobService(Protocol):
    """The job operations the API needs, the same in memory and PostgreSQL mode.

    Jobs are found by ID and scoped to `owner_id`, the signed-in GitHub account ID, or
    None without sign-in; a job owned by someone else is reported as missing. Only the
    public JobSnapshot leaves the service. `create` raises QueueFullError when full.
    """

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def create(self, target: IssueTarget, *, owner_id: int | None) -> JobSnapshot: ...
    async def get(self, job_id: str, *, owner_id: int | None) -> JobSnapshot | None: ...
    async def get_trace(self, job_id: str, *, owner_id: int | None) -> AnalysisTrace | None: ...
    async def cancel(self, job_id: str, *, owner_id: int | None) -> JobSnapshot | None: ...

    async def wait_for_update(
        self, job_id: str, *, owner_id: int | None, after: int, timeout: float,
    ) -> JobSnapshot | None:
        """Return the latest snapshot once it has events after `after` or is terminal.

        Returns by `timeout` seconds even without an update, and may return earlier, so
        callers check the snapshot again. None means the job can no longer be read.
        """
        ...


class JobManager:
    """Run analyses in this process: memory mode, the MCP server, and the Worker's runs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self.semaphore = asyncio.Semaphore(settings.concurrent_jobs)

    def _prune_cache(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self.jobs.items()):
            if job.snapshot.status in TERMINAL_STATUSES and now - job.created > self.settings.job_ttl_seconds:
                del self.jobs[job_id]
        if len(self.jobs) >= self.settings.max_jobs:
            finished = next((key for key, value in self.jobs.items() if value.snapshot.status in TERMINAL_STATUSES), None)
            if finished:
                del self.jobs[finished]
            else:
                raise QueueFullError(QUEUE_FULL)

    async def create(self, target: IssueTarget) -> Job:
        self._prune_cache()
        job = Job(queued_snapshot(target))
        self.jobs[job.snapshot.job_id] = job
        self.schedule(job)
        return job

    def schedule(
        self, job: Job, *, checkpoint: AgentCheckpoint | None = None, save_checkpoint: CheckpointWriter | None = None,
    ) -> None:
        job.task = asyncio.create_task(self._run(job, checkpoint, save_checkpoint))

    async def get(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job and job.snapshot.status in TERMINAL_STATUSES and time.monotonic() - job.created > self.settings.job_ttl_seconds:
            del self.jobs[job_id]
            job = None
        return job

    async def get_trace(self, job_id: str) -> AnalysisTrace | None:
        job = await self.get(job_id)
        if job is None:
            return None
        return AnalysisTrace(
            job_id=job_id, status=job.snapshot.status, attempts=job.trace.attempts, stop_reason=job.trace.stop_reason,
        )

    def _finish(self, job: Job, status: JobStatus, result: AnalysisResult | None, error: str | None) -> None:
        if status == "cancelled":
            job.trace.stop_reason = "job_cancelled"
            job.trace.finish("cancelled", stop_reason="job_cancelled")
        final = job.snapshot.model_copy(deep=True)
        final.status, final.result, final.error = status, result, error
        final.events.append(ProgressEvent(id=len(final.events) + 1, kind="status", message=f"Analysis {status}."))
        # In PostgreSQL mode, the Worker saves this terminal snapshot after the task ends.
        job.snapshot = final
        job.changed.set()

    async def _run(self, job: Job, checkpoint: AgentCheckpoint | None, save_checkpoint: CheckpointWriter | None) -> None:
        run: AgentRun | None = None
        result: AnalysisResult | None = None
        final_status: JobStatus = "failed"
        error: str | None = None
        try:
            async with self.semaphore:
                # A Worker's claimed job is already running in PostgreSQL.
                job.snapshot.status = "running"
                if checkpoint and checkpoint.finished:
                    result = checkpoint.result
                else:
                    if checkpoint is None or not checkpoint.result.usage.model_requests:
                        job.emit("status", "Reading evidence and investigating the issue.")
                    target = IssueTarget.from_url(job.snapshot.issue_url)
                    run = AgentRun(
                        target, self.settings, job.emit,
                        checkpoint=checkpoint, save_checkpoint=save_checkpoint, trace=job.trace,
                    )
                    elapsed = checkpoint.elapsed_seconds() if checkpoint else 0
                    remaining = self.settings.runtime_seconds - elapsed
                    if remaining <= 0:
                        raise TimeoutError
                    result = await asyncio.wait_for(run.run(), timeout=remaining)
                if result.content is not None and not result.limitations:
                    final_status = "completed"
                elif result.sources:
                    final_status = "partial"
                elif job.trace.stop_reason == "repository_not_public":
                    error = REPOSITORY_NOT_PUBLIC
                elif job.trace.stop_reason == "repository_check_failed":
                    error = REPOSITORY_CHECK_FAILED
                else:
                    error = "No issue evidence could be retrieved. Check the URL and the activity log."
        except asyncio.CancelledError:
            if job.suspended:
                raise
            final_status = "cancelled"
            if run:
                result = run.snapshot("The analysis was cancelled before completion.")
        except TimeoutError:
            final_status = "partial"
            job.trace.stop_reason = "job_runtime_limit"
            job.trace.finish("timed_out", stop_reason="job_runtime_limit")
            if run:
                result = run.snapshot("The job runtime limit was reached. Further investigation was not completed.")
        except StorageError:
            # A Worker checkpoint write failed. It is not an analysis result: the Worker
            # reconnects and resumes from the last checkpoint, or stops.
            raise
        except (AuthenticationError, RateLimitError, APITimeoutError, APIConnectionError, APIStatusError) as exc:
            if isinstance(exc, AuthenticationError):
                message = "OpenAI rejected OPENAI_API_KEY. Check the server configuration."
            elif isinstance(exc, RateLimitError):
                message = "OpenAI quota or rate limit reached. Check billing or try later."
            elif isinstance(exc, (APITimeoutError, APIConnectionError)):
                message = "Could not complete the OpenAI request. Check connectivity and try again."
            else:
                message = "OpenAI rejected the request. Check model access and server configuration."
            job.trace.stop_reason = job.trace.stop_reason or "model_request_failed"
            job.trace.finish("failed", stop_reason=job.trace.stop_reason)
            error = message
            result = run.snapshot(message) if run else None
            final_status = "partial" if result and result.sources else "failed"
        except Exception as exc:
            # Keep upstream response bodies and credentials out of API errors and logs.
            logger.error("Analysis failed with %s", type(exc).__name__)
            job.trace.stop_reason = "unexpected_error"
            job.trace.finish("failed", stop_reason="unexpected_error")
            error = "The analysis could not be completed. Check server configuration and try again."
            result = run.snapshot(error) if run else None
            final_status = "partial" if result and result.sources else "failed"
        self._finish(job, final_status, result, error)

    async def cancel(self, job: Job, *, suspend: bool = False) -> None:
        async with job.cancel_lock:
            if job.task and job.snapshot.status not in TERMINAL_STATUSES:
                job.suspended = suspend
                job.task.cancel()
                try:
                    await job.task
                except asyncio.CancelledError:
                    # Cancellation before the coroutine starts bypasses its handler.
                    if not suspend:
                        self._finish(job, "cancelled", None, None)

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            await self.cancel(job)


class MemoryJobService:
    """Memory mode for the API: this process runs the analyses, and there is no sign-in."""

    def __init__(self, settings: Settings) -> None:
        self.manager = JobManager(settings)

    @staticmethod
    def _require_no_owner(owner_id: int | None) -> None:
        # Sign-in requires PostgreSQL, so an owner here means the app was assembled wrongly.
        if owner_id is not None:
            raise ValueError("Memory mode has no accounts.")

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        await self.manager.close()

    async def create(self, target: IssueTarget, *, owner_id: int | None) -> JobSnapshot:
        self._require_no_owner(owner_id)
        return (await self.manager.create(target)).snapshot

    async def get(self, job_id: str, *, owner_id: int | None) -> JobSnapshot | None:
        self._require_no_owner(owner_id)
        job = await self.manager.get(job_id)
        return job.snapshot if job is not None else None

    async def get_trace(self, job_id: str, *, owner_id: int | None) -> AnalysisTrace | None:
        self._require_no_owner(owner_id)
        return await self.manager.get_trace(job_id)

    async def cancel(self, job_id: str, *, owner_id: int | None) -> JobSnapshot | None:
        self._require_no_owner(owner_id)
        job = await self.manager.get(job_id)
        if job is None:
            return None
        await self.manager.cancel(job)
        return job.snapshot

    async def wait_for_update(
        self, job_id: str, *, owner_id: int | None, after: int, timeout: float,
    ) -> JobSnapshot | None:
        self._require_no_owner(owner_id)
        job = await self.manager.get(job_id)
        if job is None:
            return None
        if not has_update(job.snapshot, after):
            # No await separates this check from the wait, and every change updates the
            # snapshot before setting the event, so a change cannot be missed. Readers of
            # the same job may clear the event; a waiter that was already woken stays woken.
            job.changed.clear()
            try:
                await asyncio.wait_for(job.changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
        return job.snapshot


class PostgresJobQueue:
    """PostgreSQL mode for the API: it records requests, and a separate Worker process runs them."""

    def __init__(self, settings: Settings, store: PostgresAnalysisStore) -> None:
        self.store = store
        self.capacity = settings.max_jobs
        self.account_capacity = settings.max_active_per_account

    async def start(self) -> None:
        await self.store.open()

    async def close(self) -> None:
        await self.store.close()

    async def create(self, target: IssueTarget, *, owner_id: int | None) -> JobSnapshot:
        snapshot = queued_snapshot(target)
        outcome = await self.store.enqueue(
            snapshot, self.capacity, owner_id=owner_id, account_capacity=self.account_capacity,
        )
        if outcome == "account_full":
            # One account cannot fill the queue and use up the shared model budget.
            raise AccountLimitError(ACCOUNT_LIMIT.format(limit=self.account_capacity))
        if outcome == "queue_full":
            raise QueueFullError(QUEUE_FULL)
        return snapshot

    async def get(self, job_id: str, *, owner_id: int | None) -> JobSnapshot | None:
        return await self.store.get(job_id, owner_id=owner_id)

    async def get_trace(self, job_id: str, *, owner_id: int | None) -> AnalysisTrace | None:
        return await self.store.get_trace(job_id, owner_id=owner_id)

    async def cancel(self, job_id: str, *, owner_id: int | None) -> JobSnapshot | None:
        return await self.store.request_cancel(job_id, owner_id=owner_id)

    async def wait_for_update(
        self, job_id: str, *, owner_id: int | None, after: int, timeout: float,
    ) -> JobSnapshot | None:
        # The Worker writes from another process, so read again after one poll interval.
        # Returning after each read lets the caller re-check the session between reads.
        await asyncio.sleep(min(POLL_SECONDS, timeout))
        return await self.store.get(job_id, owner_id=owner_id)
