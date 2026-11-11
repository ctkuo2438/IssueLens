"""Share bounded Agent execution across memory mode and the background Worker."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import time
from uuid import uuid4

from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, RateLimitError

from backend.app.agent import AgentRun
from backend.app.checkpoint import AgentCheckpoint, CheckpointWriter
from backend.app.config import Settings
from backend.app.github import REPOSITORY_CHECK_FAILED, REPOSITORY_NOT_PUBLIC
from backend.app.models import (
    AnalysisResult, EventKind, IssueTarget, JobSnapshot, JobStatus, ProgressEvent, TERMINAL_STATUSES,
)
from backend.app.storage import AnalysisStore, PostgresAnalysisStore, StorageError
from backend.app.trace import AnalysisTrace, TraceLog


logger = logging.getLogger(__name__)
QUEUE_FULL = "The local queue is full. Wait for an analysis to finish."


class QueueFullError(Exception):
    """The local job store has reached its capacity."""


@dataclass
class Job:
    snapshot: JobSnapshot
    created: float = field(default_factory=time.monotonic)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    finalizing: bool = False
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


def queued_job(target: IssueTarget) -> Job:
    job = Job(JobSnapshot(
        job_id=uuid4().hex, issue_url=target.url,
        status="queued", created_at=datetime.now(UTC),
    ))
    job.emit("status", "Analysis queued.")
    return job


class JobManager:
    def __init__(self, settings: Settings, store: AnalysisStore | None = None) -> None:
        self.settings = settings
        self.store = store
        self.jobs: dict[str, Job] = {}
        self.semaphore = asyncio.Semaphore(settings.concurrent_jobs)
        self.creation_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.store is not None:
            await self.store.open()

    async def _save(self, snapshot: JobSnapshot) -> None:
        if self.store is not None:
            await self.store.save(snapshot)

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
        async with self.creation_lock:
            self._prune_cache()
            job = queued_job(target)
            # A configured database must accept the job before any paid work starts.
            await self._save(job.snapshot)
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
        if job is not None:
            return job
        if self.store is not None:
            snapshot = await self.store.get(job_id)
            if snapshot is not None:
                # Historical reads do not consume active execution/cache capacity.
                return Job(snapshot)
        return None

    async def get_trace(self, job_id: str) -> AnalysisTrace | None:
        job = await self.get(job_id)
        if job is None:
            return None
        return AnalysisTrace(
            job_id=job_id, status=job.snapshot.status, attempts=job.trace.attempts, stop_reason=job.trace.stop_reason,
        )

    async def _finish(self, job: Job, status: JobStatus, result: AnalysisResult | None, error: str | None) -> None:
        job.finalizing = True
        if status == "cancelled":
            job.trace.stop_reason = "job_cancelled"
            job.trace.finish("cancelled", stop_reason="job_cancelled")
        final = job.snapshot.model_copy(deep=True)
        final.status, final.result, final.error = status, result, error
        final.events.append(ProgressEvent(id=len(final.events) + 1, kind="status", message=f"Analysis {status}."))
        try:
            # Publish the terminal snapshot only after its transaction commits.
            await self._save(final)
        except StorageError:
            logger.error("The final analysis snapshot could not be saved.")
            final.status = "partial" if result is not None else "failed"
            final.error = "The analysis could not be saved to PostgreSQL. This result is available only in this backend's memory; do not rely on it surviving a restart."
            final.events[-1] = ProgressEvent(id=len(final.events), kind="warning", message=final.error)
            final.events.append(ProgressEvent(id=len(final.events) + 1, kind="status", message=f"Analysis {final.status}."))
        job.snapshot = final
        job.changed.set()

    async def _run(self, job: Job, checkpoint: AgentCheckpoint | None, save_checkpoint: CheckpointWriter | None) -> None:
        run: AgentRun | None = None
        result: AnalysisResult | None = None
        final_status: JobStatus = "failed"
        error: str | None = None
        try:
            async with self.semaphore:
                if job.snapshot.status == "queued":
                    running = job.snapshot.model_copy(deep=True)
                    running.status = "running"
                    await self._save(running)
                    job.snapshot = running
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
            if save_checkpoint is not None:
                raise
            job.trace.stop_reason = "storage_unavailable"
            error = "The analysis could not start because PostgreSQL storage is unavailable."
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
        await self._finish(job, final_status, result, error)

    async def cancel(self, job: Job, *, suspend: bool = False) -> None:
        async with job.cancel_lock:
            if job.task and job.snapshot.status not in TERMINAL_STATUSES:
                if not job.finalizing:
                    job.suspended = suspend
                    job.task.cancel()
                try:
                    await job.task
                except asyncio.CancelledError:
                    # Cancellation before the coroutine starts bypasses its handler.
                    if not suspend:
                        await self._finish(job, "cancelled", None, None)

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            await self.cancel(job)
        if self.store is not None:
            await self.store.close()


class PostgresJobQueue:
    """API-side job operations. Execution belongs to a separate Worker process."""

    def __init__(self, settings: Settings, store: PostgresAnalysisStore) -> None:
        self.store = store
        self.capacity = settings.max_jobs

    async def start(self) -> None:
        await self.store.open()

    async def close(self) -> None:
        await self.store.close()

    async def create(self, target: IssueTarget, *, owner_id: int | None = None) -> Job:
        job = queued_job(target)
        if not await self.store.enqueue(job.snapshot, self.capacity, owner_id=owner_id):
            raise QueueFullError(QUEUE_FULL)
        return job

    async def get(self, job_id: str, *, owner_id: int | None = None) -> Job | None:
        snapshot = await self.store.get(job_id, owner_id=owner_id)
        return Job(snapshot) if snapshot is not None else None

    async def get_trace(self, job_id: str, *, owner_id: int | None = None) -> AnalysisTrace | None:
        return await self.store.get_trace(job_id, owner_id=owner_id)

    async def cancel(self, job: Job, *, owner_id: int | None = None) -> None:
        snapshot = await self.store.request_cancel(job.snapshot.job_id, owner_id=owner_id)
        if snapshot is not None:
            job.snapshot = snapshot
