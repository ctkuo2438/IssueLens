"""Public execution metadata for attempts charged to an analysis."""

from datetime import UTC, datetime
import time
from typing import Literal

from pydantic import AwareDatetime, Field, PrivateAttr

from backend.app.checkpoint import ScheduledRetry
from backend.app.models import ApiModel, JobStatus


AttemptKind = Literal["model", "tool"]
AttemptOutcome = Literal[
    "running", "succeeded", "partial", "failed", "incomplete", "rejected", "interrupted", "cancelled", "timed_out",
]


class TraceAttempt(ApiModel):
    id: int
    kind: AttemptKind
    name: str
    attempt_number: int
    is_retry: bool
    retry_of: int | None = None
    started_at: AwareDatetime
    duration_ms: int | None = None
    outcome: AttemptOutcome = "running"
    reason: str | None = None
    stop_reason: str | None = None
    retry_scheduled_for: AwareDatetime | None = None


class TraceLog(ApiModel):
    attempts: list[TraceAttempt] = Field(default_factory=list)
    stop_reason: str | None = None
    _started: float | None = PrivateAttr(default=None)

    def begin(self, kind: AttemptKind, name: str, number: int, retry: ScheduledRetry | None) -> None:
        self._started = time.monotonic()
        self.attempts.append(TraceAttempt(
            id=len(self.attempts) + 1, kind=kind, name=name, attempt_number=number,
            is_retry=retry is not None, retry_of=retry.attempt_id if retry else None,
            started_at=datetime.now(UTC),
        ))

    def finish(
        self, outcome: AttemptOutcome, *, reason: str | None = None, stop_reason: str | None = None,
        retry_scheduled_for: datetime | None = None,
    ) -> int | None:
        if self._started is None:
            return None
        attempt = self.attempts[-1]
        attempt.duration_ms = round((time.monotonic() - self._started) * 1000)
        attempt.outcome = outcome
        attempt.reason = reason
        attempt.stop_reason = stop_reason
        attempt.retry_scheduled_for = retry_scheduled_for
        self._started = None
        return attempt.id

    def mark_interrupted(self) -> bool:
        if not self.attempts or self.attempts[-1].outcome != "running":
            return False
        attempt = self.attempts[-1]
        attempt.outcome = "interrupted"
        attempt.reason = "The Worker stopped before saving the attempt result."
        attempt.stop_reason = "retry_exhausted" if attempt.is_retry else "worker_interrupted"
        return True


class AnalysisTrace(TraceLog):
    job_id: str
    status: JobStatus
