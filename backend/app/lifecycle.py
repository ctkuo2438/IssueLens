"""The analysis status lifecycle: which status changes are allowed and how each is recorded.

Statuses change in the API process (memory mode), in the Worker and in PostgreSQL
storage. All of them go through start() and finish(), so the allowed changes are
listed once and every change leaves the same Activity events. A stored record that
this version cannot read is not a status change: storage replaces it with a new
failed record, because its old status is unknown.
"""

from datetime import UTC, datetime
from uuid import uuid4

from backend.app.models import EventKind, IssueTarget, JobSnapshot, JobStatus, ProgressEvent, TERMINAL_STATUSES


ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    # A queued analysis starts, or is cancelled before it starts.
    "queued": frozenset({"running", "cancelled"}),
    # A run ends once. A run interrupted without a usable checkpoint ends as cancelled.
    "running": frozenset({"completed", "partial", "failed", "cancelled"}),
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class InvalidTransition(RuntimeError):
    """Code asked for a status change the lifecycle does not allow. It is a bug, not bad input."""


def record(snapshot: JobSnapshot, kind: EventKind, message: str) -> None:
    """Append an Activity event. Event IDs start at 1 and increase by one."""
    next_id = snapshot.events[-1].id + 1 if snapshot.events else 1
    snapshot.events.append(ProgressEvent(id=next_id, kind=kind, message=message))


def queued_snapshot(target: IssueTarget) -> JobSnapshot:
    """Every analysis starts here."""
    snapshot = JobSnapshot(job_id=uuid4().hex, issue_url=target.url, status="queued", created_at=datetime.now(UTC))
    record(snapshot, "status", "Analysis queued.")
    return snapshot


def _changed(snapshot: JobSnapshot, status: JobStatus) -> JobSnapshot:
    if status not in ALLOWED_TRANSITIONS[snapshot.status]:
        raise InvalidTransition(f"An analysis cannot change from {snapshot.status} to {status}.")
    changed = snapshot.model_copy(deep=True)
    changed.status = status
    return changed


def start(snapshot: JobSnapshot) -> JobSnapshot:
    """Return a running copy of a queued snapshot.

    It records no event, because the run's own progress events follow.
    """
    return _changed(snapshot, "running")


def finish(
    snapshot: JobSnapshot, status: JobStatus, *,
    error: str | None = None, warning: str | None = None, message: str | None = None,
) -> JobSnapshot:
    """Return a copy of the snapshot in a final status.

    It sets `error`, records `warning` when one is given, and then records a status
    event: `message`, or "Analysis <status>." by default. The original is unchanged.
    """
    if status not in TERMINAL_STATUSES:
        raise InvalidTransition(f"{status} is not a final status.")
    final = _changed(snapshot, status)
    final.error = error
    if warning is not None:
        record(final, "warning", warning)
    record(final, "status", message or f"Analysis {status}.")
    return final
