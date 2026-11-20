"""Store analysis snapshots, checkpoints and traces and coordinate the job queue."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from typing import Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from backend.app.checkpoint import AgentCheckpoint
from backend.app.models import GitHubUser, IssueTarget, JobSnapshot, JobStatus, ProgressEvent
from backend.app.trace import AnalysisTrace, TraceLog


logger = logging.getLogger(__name__)


STORAGE_UNAVAILABLE = "PostgreSQL storage is unavailable. Check DATABASE_URL and the database service."
INTERRUPTED = "The previous analysis process stopped before this analysis finished. Start a new analysis to investigate again."
INVALID_CHECKPOINT = "The saved checkpoint is incompatible with this Worker. Start a new analysis to investigate again."
INCOMPATIBLE_RECORD = "This saved analysis cannot be read by the current version and was stopped. Start a new analysis to investigate again."
WORKER_LOCK = 0x49737375654C656E  # Reuse the lock held by the old in-process backend.
SCHEMA_LOCK = WORKER_LOCK + 1
ENQUEUE_LOCK = WORKER_LOCK + 2
POLL_SECONDS = 0.25
EnqueueOutcome = Literal["queued", "queue_full", "account_full"]
# One limit for a connection attempt, a pool wait and the pool's reconnect window.
CONNECT_SECONDS = 3
# After a database restart every idle connection is stale. The pool waits about 0 and
# then 1 second between failed checks, so with two connections a request still gets a
# fresh one within CONNECT_SECONDS. Two connections are enough for one API process.
API_CONNECTIONS = 2
CONNECTION_OPTIONS = {
    "autocommit": True, "connect_timeout": CONNECT_SECONDS,
    # Detect a silently dropped network path instead of waiting for the operating
    # system's retransmission limit. tcp_user_timeout has no effect where the OS lacks it.
    "keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5, "keepalives_count": 3,
    "tcp_user_timeout": 15_000,
    # Server-side keepalives let PostgreSQL end a dead session, and release the Worker
    # lock it holds, instead of keeping both until the OS gives up hours later.
    "options": (
        "-c statement_timeout=5000 -c lock_timeout=3000 "
        "-c tcp_keepalives_idle=10 -c tcp_keepalives_interval=5 -c tcp_keepalives_count=3"
    ),
}


class ExceptionTypeOnly(logging.Filter):
    """Log exceptions passed as arguments by type only.

    libpq messages can repeat parts of the connection URL, including a password.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(type(arg).__name__ if isinstance(arg, BaseException) else arg for arg in record.args)
        return True


# psycopg_pool logs each failed connection attempt with the libpq error text.
logging.getLogger("psycopg.pool").addFilter(ExceptionTypeOnly())


class StorageError(RuntimeError):
    """A sanitized storage failure that is safe to return through the API."""


class StorageConnectionError(StorageError):
    """The connection could not be made or was lost, so reconnecting may succeed."""


class WorkerLockHeld(StorageError):
    """Another session holds the Worker lock, possibly the server's copy of a dropped connection."""


def interrupted_snapshot(snapshot: JobSnapshot, reason: str = INTERRUPTED) -> JobSnapshot:
    """Preserve available evidence and make an abandoned record terminal."""
    restored = snapshot.model_copy(deep=True)
    restored.status = "cancelled"
    restored.error = reason
    next_id = max((event.id for event in restored.events), default=0) + 1
    restored.events.extend([
        ProgressEvent(id=next_id, kind="warning", message=reason),
        ProgressEvent(id=next_id + 1, kind="status", message="Analysis cancelled after an interruption."),
    ])
    return restored


def incompatible_snapshot(job_id: str, stored: object) -> JobSnapshot:
    """A terminal snapshot for a stored record that no longer validates, keeping readable fields."""
    fields = stored if isinstance(stored, dict) else {}
    issue_url, created_at = "", datetime.now(UTC)
    try:
        if isinstance(fields.get("issue_url"), str):
            issue_url = IssueTarget.from_url(fields["issue_url"]).url
    except ValueError:
        pass
    try:
        if isinstance(fields.get("created_at"), str):
            parsed = datetime.fromisoformat(fields["created_at"])
            created_at = parsed if parsed.tzinfo is not None else created_at
    except ValueError:
        pass
    return JobSnapshot(
        job_id=job_id, issue_url=issue_url, status="failed", created_at=created_at, error=INCOMPATIBLE_RECORD,
        events=[
            ProgressEvent(id=1, kind="warning", message=INCOMPATIBLE_RECORD),
            ProgressEvent(id=2, kind="status", message="Analysis failed."),
        ],
    )


@dataclass
class ClaimedAnalysis:
    snapshot: JobSnapshot
    checkpoint: AgentCheckpoint
    resumed: bool = False
    trace: TraceLog = field(default_factory=TraceLog)


class PostgresAnalysisStore:
    """PostgreSQL access for the API (a reconnecting pool) or the Worker (one lock-holding connection)."""

    def __init__(self, database_url: str, *, auth_enabled: bool = False) -> None:
        self._database_url = database_url
        self._auth_enabled = auth_enabled
        self._pool: AsyncConnectionPool | None = None
        self._connection: psycopg.AsyncConnection | None = None
        self._lock = asyncio.Lock()
        self._schema_ready = False

    async def open(self, *, worker: bool = False) -> None:
        if self._pool is not None or self._connection is not None:
            # One instance is the API pool or the Worker's lock connection, never both.
            raise RuntimeError("This store is already open.")
        if not worker:
            await self._open_pool()
            return
        # The Worker's ownership is a session advisory lock on this one connection.
        # If the connection drops, the lock is gone: the Worker suspends its runs and
        # reopens the store, and checkpoints let the claimed runs resume.
        connection: psycopg.AsyncConnection | None = None
        try:
            connection = await psycopg.AsyncConnection.connect(self._database_url, **CONNECTION_OPTIONS)
            cursor = await connection.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,))
            owner = await cursor.fetchone()
            if not owner or not owner[0]:
                raise WorkerLockHeld("Another IssueLens Worker or legacy backend already owns this database.")
            await self._prepare_schema(connection)
            async with connection.transaction():
                cursor = await connection.execute("""
                    SELECT job_id, snapshot, trace FROM issuelens_analyses
                    WHERE snapshot->>'status' = 'running' AND checkpoint IS NULL
                    FOR UPDATE
                """)
                for job_id, stored_snapshot, stored_trace in await cursor.fetchall():
                    try:
                        snapshot = JobSnapshot.model_validate(stored_snapshot)
                        trace = TraceLog.model_validate(stored_trace)
                    except ValidationError:
                        await self._retire_incompatible(connection, job_id, stored_snapshot, stored_trace)
                        continue
                    trace.mark_interrupted()
                    trace.stop_reason = "checkpoint_unavailable"
                    await self._update(connection, interrupted_snapshot(snapshot), "running", trace=trace)
            self._connection = connection
        except BaseException as exc:
            lost = connection is None or connection.closed
            if connection is not None:
                await connection.close()
            if isinstance(exc, psycopg.OperationalError) and lost:
                raise StorageConnectionError(STORAGE_UNAVAILABLE) from None
            if isinstance(exc, (psycopg.Error, ValueError, TypeError)):
                raise StorageError(STORAGE_UNAVAILABLE) from None
            raise

    async def _open_pool(self) -> None:
        # The pool checks each connection before use and replaces dropped ones, so
        # the API recovers after a database restart. Startup still fails fast.
        try:
            # Parse first: libpq parse errors repeat the URL text, so they must not
            # reach the pool's retry log.
            conninfo_to_dict(self._database_url)
        except psycopg.Error:
            raise StorageError(STORAGE_UNAVAILABLE) from None
        pool = AsyncConnectionPool(
            # connection_class is the default; passing it lets Pyright infer the pool type.
            self._database_url, connection_class=psycopg.AsyncConnection,
            kwargs=dict(CONNECTION_OPTIONS), min_size=1, max_size=API_CONNECTIONS,
            open=False, check=AsyncConnectionPool.check_connection, timeout=CONNECT_SECONDS,
            # A short reconnect window ends a failed attempt quickly, so the next
            # request starts a fresh one instead of waiting for a long backoff.
            reconnect_timeout=CONNECT_SECONDS, name="issuelens-api",
        )
        try:
            await pool.open(wait=True, timeout=CONNECT_SECONDS)
            async with pool.connection() as connection:
                await self._prepare_schema(connection)
        except BaseException as exc:
            await pool.close()
            if isinstance(exc, (psycopg.Error, ValueError, TypeError)):
                raise StorageError(STORAGE_UNAVAILABLE) from None
            raise
        self._pool = pool

    async def _prepare_schema(self, connection: psycopg.AsyncConnection) -> None:
        """Create or upgrade the tables the first time this store opens.

        A Worker reconnect skips it, because CREATE INDEX IF NOT EXISTS takes a table
        lock that would compete with the API's writes during recovery.
        """
        if self._schema_ready:
            return
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS issuelens_analyses (
                    job_id text PRIMARY KEY,
                    snapshot jsonb NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (jsonb_typeof(snapshot) = 'object'),
                    CHECK (snapshot->>'job_id' = job_id)
                )
            """)
            columns = await connection.execute("""
                SELECT attname FROM pg_attribute
                WHERE attrelid = 'issuelens_analyses'::regclass
                  AND attname IN ('cancel_requested', 'checkpoint', 'trace', 'owner_github_id', 'incompatible_record')
                  AND NOT attisdropped
            """)
            present = {row[0] for row in await columns.fetchall()}
            if "cancel_requested" not in present:
                await connection.execute("""
                    ALTER TABLE issuelens_analyses
                    ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false
                """)
            if "checkpoint" not in present:
                # Keep the exact JSON replay order used by the local token estimator.
                await connection.execute("ALTER TABLE issuelens_analyses ADD COLUMN checkpoint text")
            if "trace" not in present:
                await connection.execute("""
                    ALTER TABLE issuelens_analyses ADD COLUMN trace jsonb NOT NULL DEFAULT '{}'::jsonb
                """)
            if "owner_github_id" not in present:
                await connection.execute("""
                    ALTER TABLE issuelens_analyses ADD COLUMN owner_github_id bigint CHECK (owner_github_id > 0)
                """)
            if "incompatible_record" not in present:
                # The original snapshot and trace of a record this version could not read.
                await connection.execute("ALTER TABLE issuelens_analyses ADD COLUMN incompatible_record jsonb")
            await connection.execute("""
                CREATE INDEX IF NOT EXISTS issuelens_analyses_pending
                ON issuelens_analyses (updated_at, job_id)
                WHERE snapshot->>'status' IN ('queued', 'running')
            """)
            if self._auth_enabled:
                # Login attempts live in browser cookies; only completed ones are recorded here,
                # so each attempt creates at most one session. Older databases may still have
                # an unused issuelens_oauth_requests table, which is left untouched.
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS issuelens_used_logins (
                        state_hash text PRIMARY KEY,
                        expires_at timestamptz NOT NULL
                    )
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS issuelens_used_logins_expiry ON issuelens_used_logins (expires_at)
                """)
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS issuelens_sessions (
                        token_hash text PRIMARY KEY,
                        github_id bigint NOT NULL CHECK (github_id > 0),
                        github_login text NOT NULL,
                        expires_at timestamptz NOT NULL
                    )
                """)
        self._schema_ready = True

    @staticmethod
    async def _update(
        connection: psycopg.AsyncConnection, snapshot: JobSnapshot, previous_status: JobStatus,
        checkpoint: AgentCheckpoint | None = None, *, trace: TraceLog | None = None,
    ) -> None:
        running = snapshot.status == "running"
        cursor = await connection.execute("""
            UPDATE issuelens_analyses
            SET snapshot = %s, updated_at = CURRENT_TIMESTAMP,
                cancel_requested = cancel_requested AND %s,
                checkpoint = CASE WHEN %s THEN COALESCE(%s, checkpoint) ELSE NULL END,
                trace = COALESCE(%s, trace)
            WHERE job_id = %s AND snapshot->>'status' = %s
        """, (
            Jsonb(snapshot.model_dump(mode="json")), running, running,
            checkpoint.model_dump_json() if checkpoint else None,
            Jsonb(trace.model_dump(mode="json")) if trace is not None else None, snapshot.job_id, previous_status,
        ))
        if cursor.rowcount != 1:
            raise StorageError("The analysis changed before the Worker could save it.")

    @staticmethod
    async def _retire_incompatible(
        connection: psycopg.AsyncConnection, job_id: str, stored_snapshot: object, stored_trace: object,
    ) -> None:
        """Make a record this version cannot read terminal, so it never blocks the queue.

        The caller holds the row lock. The original JSON is kept for debugging.
        """
        await connection.execute("""
            UPDATE issuelens_analyses
            SET snapshot = %s, trace = %s, incompatible_record = %s,
                checkpoint = NULL, cancel_requested = false, updated_at = CURRENT_TIMESTAMP
            WHERE job_id = %s
        """, (
            Jsonb(incompatible_snapshot(job_id, stored_snapshot).model_dump(mode="json")),
            Jsonb(TraceLog(stop_reason="record_incompatible").model_dump(mode="json")),
            Jsonb({"snapshot": stored_snapshot, "trace": stored_trace}), job_id,
        ))
        logger.warning("Stopped stored analysis %s because this version cannot read it.", job_id)

    def _connected(self) -> psycopg.AsyncConnection:
        if self._connection is None or self._connection.closed:
            raise StorageConnectionError(STORAGE_UNAVAILABLE)
        return self._connection

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[psycopg.AsyncConnection]:
        if self._pool is not None:
            try:
                # A broken connection is discarded when it returns to the pool.
                async with self._pool.connection() as connection:
                    yield connection
            except (psycopg.Error, ValueError, TypeError):
                raise StorageError(STORAGE_UNAVAILABLE) from None
            return
        async with self._lock:
            connection = self._connected()
            try:
                yield connection
            except (psycopg.Error, ValueError, TypeError) as exc:
                # Only a closed connection means the Worker lost its lock. Other failures,
                # such as stored data that no longer validates, would repeat after a reconnect.
                if isinstance(exc, psycopg.OperationalError) and connection.closed:
                    raise StorageConnectionError(STORAGE_UNAVAILABLE) from None
                raise StorageError(STORAGE_UNAVAILABLE) from None

    async def get(self, job_id: str, *, owner_id: int | None = None) -> JobSnapshot | None:
        async with self._operation() as connection:
            cursor = await connection.execute("""
                SELECT snapshot FROM issuelens_analyses
                WHERE job_id = %s AND owner_github_id IS NOT DISTINCT FROM %s
            """, (job_id, owner_id))
            row = await cursor.fetchone()
            return JobSnapshot.model_validate(row[0]) if row else None

    async def get_trace(self, job_id: str, *, owner_id: int | None = None) -> AnalysisTrace | None:
        async with self._operation() as connection:
            cursor = await connection.execute("""
                SELECT snapshot->>'status', trace FROM issuelens_analyses
                WHERE job_id = %s AND owner_github_id IS NOT DISTINCT FROM %s
            """, (job_id, owner_id))
            row = await cursor.fetchone()
            return AnalysisTrace.model_validate({**row[1], "job_id": job_id, "status": row[0]}) if row else None

    async def enqueue(
        self, snapshot: JobSnapshot, capacity: int, *, owner_id: int | None = None, account_capacity: int | None = None,
    ) -> EnqueueOutcome:
        """Insert a queued analysis unless the shared queue or the owner's share is full."""
        async with self._operation() as connection, connection.transaction():
            # One lock orders every create, so both limits hold across API processes.
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (ENQUEUE_LOCK,))
            cursor = await connection.execute("""
                SELECT count(*), count(*) FILTER (WHERE owner_github_id = %s)
                FROM issuelens_analyses WHERE snapshot->>'status' IN ('queued', 'running')
            """, (owner_id,))
            counts = await cursor.fetchone()
            if counts is None:
                return "queue_full"
            active, owned = counts
            # Local mode has no owner, so only the shared limit applies. The account limit
            # is checked first because the user can act on it by waiting for their own run.
            if owner_id is not None and account_capacity is not None and owned >= account_capacity:
                return "account_full"
            if active >= capacity:
                return "queue_full"
            await connection.execute(
                "INSERT INTO issuelens_analyses (job_id, snapshot, owner_github_id) VALUES (%s, %s, %s)",
                (snapshot.job_id, Jsonb(snapshot.model_dump(mode="json")), owner_id),
            )
            return "queued"

    async def claim(self, active_job_ids: list[str], signature: str) -> ClaimedAnalysis | None:
        async with self._operation() as connection, connection.transaction():
            cursor = await connection.execute("""
                SELECT job_id, snapshot, checkpoint, cancel_requested, trace FROM issuelens_analyses
                WHERE snapshot->>'status' IN ('queued', 'running') AND NOT (job_id = ANY(%s))
                ORDER BY (snapshot->>'status' = 'running') DESC, updated_at, job_id
                LIMIT 1 FOR UPDATE
            """, (active_job_ids,))
            row = await cursor.fetchone()
            if row is None:
                return None
            job_id, stored_snapshot, stored_checkpoint, cancel_requested, stored_trace = row
            try:
                snapshot = JobSnapshot.model_validate(stored_snapshot)
                trace = TraceLog.model_validate(stored_trace)
            except ValidationError:
                # Running rows are claimed first, so an unreadable one would block the whole queue.
                await self._retire_incompatible(connection, job_id, stored_snapshot, stored_trace)
                return None
            if snapshot.status == "queued":
                checkpoint = AgentCheckpoint.start(signature)
                snapshot.status = "running"
                await self._update(connection, snapshot, "queued", checkpoint)
                return ClaimedAnalysis(snapshot, checkpoint)

            interrupted = trace.mark_interrupted()
            try:
                checkpoint = AgentCheckpoint.model_validate_json(stored_checkpoint)
            except ValueError:
                trace.stop_reason = "checkpoint_incompatible"
                await self._update(connection, interrupted_snapshot(snapshot, INVALID_CHECKPOINT), "running", trace=trace)
                return None
            if cancel_requested:
                snapshot.result = checkpoint.result
                if not checkpoint.finished:
                    snapshot.result.usage.elapsed_seconds = round(checkpoint.elapsed_seconds(), 2)
                if checkpoint.tools:
                    snapshot.result.limitations.extend(checkpoint.tools.limitations)
                snapshot.result.limitations.append("The analysis was cancelled before completion.")
                self._cancel_snapshot(snapshot)
                trace.stop_reason = "job_cancelled"
                await self._update(connection, snapshot, "running", trace=trace)
                return None
            if not checkpoint.finished and checkpoint.signature != signature:
                snapshot.result = checkpoint.result
                trace.stop_reason = "checkpoint_incompatible"
                await self._update(connection, interrupted_snapshot(snapshot, INVALID_CHECKPOINT), "running", trace=trace)
                return None
            if interrupted:
                await self._update(connection, snapshot, "running", trace=trace)
            return ClaimedAnalysis(snapshot, checkpoint, resumed=True, trace=trace)

    async def save_running(
        self, snapshot: JobSnapshot, checkpoint: AgentCheckpoint | None = None, *, trace: TraceLog | None = None,
    ) -> None:
        async with self._operation() as connection:
            await self._update(connection, snapshot, "running", checkpoint, trace=trace)

    @staticmethod
    def _cancel_snapshot(snapshot: JobSnapshot) -> None:
        snapshot.status = "cancelled"
        snapshot.events.append(ProgressEvent(
            id=len(snapshot.events) + 1, kind="status", message="Analysis cancelled.",
        ))

    async def request_cancel(self, job_id: str, *, owner_id: int | None = None) -> JobSnapshot | None:
        async with self._operation() as connection, connection.transaction():
            cursor = await connection.execute("""
                SELECT snapshot FROM issuelens_analyses
                WHERE job_id = %s AND owner_github_id IS NOT DISTINCT FROM %s FOR UPDATE
            """, (job_id, owner_id))
            row = await cursor.fetchone()
            if row is None:
                return None
            snapshot = JobSnapshot.model_validate(row[0])
            if snapshot.status == "queued":
                self._cancel_snapshot(snapshot)
                await self._update(connection, snapshot, "queued", trace=TraceLog(stop_reason="job_cancelled"))
            elif snapshot.status == "running":
                await connection.execute(
                    "UPDATE issuelens_analyses SET cancel_requested = true WHERE job_id = %s", (job_id,),
                )
            return snapshot

    async def cancellation_requests(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        async with self._operation() as connection:
            cursor = await connection.execute("""
                SELECT job_id FROM issuelens_analyses WHERE job_id = ANY(%s) AND cancel_requested
            """, (job_ids,))
            return [row[0] for row in await cursor.fetchall()]

    async def login_used(self, state_hash: str) -> bool:
        """Whether a login attempt already created a session. A read, so anonymous calls write nothing."""
        async with self._operation() as connection:
            cursor = await connection.execute(
                "SELECT 1 FROM issuelens_used_logins WHERE state_hash = %s", (state_hash,),
            )
            return await cursor.fetchone() is not None

    async def create_session(
        self, token_hash: str, user: GitHubUser, lifetime: timedelta, previous_hash: str | None,
        *, login_hash: str, login_lifetime: timedelta,
    ) -> bool:
        """Create a session unless this login attempt already created one."""
        async with self._operation() as connection, connection.transaction():
            # Only GitHub-confirmed logins reach this write, so the table grows with real sign-ins.
            await connection.execute("DELETE FROM issuelens_used_logins WHERE expires_at <= CURRENT_TIMESTAMP")
            cursor = await connection.execute("""
                INSERT INTO issuelens_used_logins (state_hash, expires_at) VALUES (%s, CURRENT_TIMESTAMP + %s)
                ON CONFLICT (state_hash) DO NOTHING
            """, (login_hash, login_lifetime))
            if cursor.rowcount != 1:
                return False  # Another callback completed this attempt first.
            await connection.execute("""
                DELETE FROM issuelens_sessions WHERE expires_at <= CURRENT_TIMESTAMP OR token_hash = %s
            """, (previous_hash,))
            await connection.execute("""
                INSERT INTO issuelens_sessions (token_hash, github_id, github_login, expires_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP + %s)
            """, (token_hash, user.id, user.login, lifetime))
            return True

    async def session_user(self, token_hash: str) -> GitHubUser | None:
        async with self._operation() as connection:
            cursor = await connection.execute("""
                SELECT github_id, github_login FROM issuelens_sessions
                WHERE token_hash = %s AND expires_at > CURRENT_TIMESTAMP
            """, (token_hash,))
            row = await cursor.fetchone()
            return GitHubUser(id=row[0], login=row[1]) if row else None

    async def delete_session(self, token_hash: str) -> None:
        async with self._operation() as connection:
            await connection.execute("DELETE FROM issuelens_sessions WHERE token_hash = %s", (token_hash,))

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
