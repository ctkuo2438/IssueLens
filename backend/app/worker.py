"""Run queued analyses in a process independent of the HTTP server."""

import asyncio
import signal
import time

from backend.app.agent import checkpoint_signature
from backend.app.checkpoint import AgentCheckpoint, CheckpointWriter
from backend.app.config import Settings
from backend.app.jobs import Job, JobManager
from backend.app.models import TERMINAL_STATUSES
from backend.app.storage import (
    POLL_SECONDS, PostgresAnalysisStore, StorageConnectionError, StorageError, WorkerLockHeld,
)


RECONNECT_SECONDS = 1.0
# A failed attempt is cheap, so a short cap keeps recovery prompt after PostgreSQL returns.
MAX_RECONNECT_SECONDS = 5.0
# A Worker that served this long before losing PostgreSQL starts over at the shortest delay.
STABLE_SECONDS = 60.0


class AnalysisWorker:
    def __init__(self, settings: Settings) -> None:
        self.store = PostgresAnalysisStore(settings.database_url)
        self.runner = JobManager(settings)
        self.signature = checkpoint_signature(settings)

    def _checkpoint_writer(self, job: Job) -> CheckpointWriter:
        async def save(checkpoint: AgentCheckpoint) -> None:
            # Read job.snapshot at each save: a status change replaces the snapshot object.
            await self.store.save_running(job.snapshot, checkpoint, trace=job.trace)

        return save

    async def _publish(self, active: dict[str, Job]) -> None:
        for job_id, job in list(active.items()):
            if job.task is not None and job.task.done() and not job.task.cancelled():
                job.task.result()
            if job.snapshot.status in TERMINAL_STATUSES:
                await self.store.save_running(job.snapshot, trace=job.trace)
                del active[job_id]

    async def run(self, stop: asyncio.Event) -> None:
        # Startup fails fast, like the API, so a configuration error is shown at once.
        await self.store.open(worker=True)
        delay = RECONNECT_SECONDS
        while True:
            served = time.monotonic()
            try:
                await self._serve(stop)
                return
            except StorageConnectionError as exc:
                # A failed final save during shutdown must still end the process with an error.
                if stop.is_set():
                    raise
                print(str(exc), flush=True)
            # Other StorageErrors, such as stored data that no longer validates, would repeat
            # after a reconnect, so they stop the Worker. A lost connection also lost the
            # ownership lock: reopening reacquires it and claimed runs resume from checkpoints.
            if time.monotonic() - served >= STABLE_SECONDS:
                delay = RECONNECT_SECONDS
            next_delay = await self._reopen(stop, delay)
            if next_delay is None:
                return
            delay = next_delay

    async def _reopen(self, stop: asyncio.Event, delay: float) -> float | None:
        """Retry with doubling delays until the store reopens.

        Returns the delay for the next reconnect, so a connection that drops again soon
        keeps backing off. None means a stop was requested.
        """
        while True:
            print(f"Reconnecting to PostgreSQL in {delay:g} seconds.", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return None
            except TimeoutError:
                pass
            delay = min(MAX_RECONNECT_SECONDS, delay * 2)
            try:
                await self.store.open(worker=True)
                return delay
            except (StorageConnectionError, WorkerLockHeld) as exc:
                # The server may still hold the lock for the dropped session until it notices.
                print(str(exc), flush=True)

    async def _serve(self, stop: asyncio.Event) -> None:
        active: dict[str, Job] = {}
        print("IssueLens Worker is ready.", flush=True)
        try:
            try:
                while not stop.is_set():
                    for job_id in await self.store.cancellation_requests(list(active)):
                        await self.runner.cancel(active[job_id])
                    await self._publish(active)

                    while len(active) < self.runner.settings.concurrent_jobs and not stop.is_set():
                        claimed = await self.store.claim(list(active), self.signature)
                        if claimed is None:
                            break
                        job = Job(claimed.snapshot, trace=claimed.trace)
                        if claimed.resumed:
                            usage = claimed.checkpoint.result.usage
                            job.emit("status", (
                                f"Resumed from a checkpoint after {usage.model_requests} model attempts "
                                f"and {usage.tool_calls} tool attempts."
                            ))
                        active[job.snapshot.job_id] = job
                        self.runner.schedule(
                            job, checkpoint=claimed.checkpoint, save_checkpoint=self._checkpoint_writer(job),
                        )

                    try:
                        await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
                    except TimeoutError:
                        pass
            finally:
                # Only unfinished analyses are suspended; finished ones have nothing to stop.
                running =[job for job in active.values() if job.snapshot.status not in TERMINAL_STATUSES]
                await asyncio.gather(*(self.runner.cancel(job, suspend=True) for job in running), return_exceptions=True)
                if running:
                    print(f"Suspended {len(running)} running analyses; their checkpoints are kept.", flush=True)
            # Completed results can be published while suspended jobs keep their checkpoints.
            await self._publish(active)
        finally:
            await self.store.close()


async def serve(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await AnalysisWorker(settings).run(stop)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    settings = Settings.from_env()
    if not settings.database_url:
        raise SystemExit("Set DATABASE_URL before starting the IssueLens Worker.")
    if not settings.openai_api_key:
        raise SystemExit("Set OPENAI_API_KEY before starting the IssueLens Worker.")
    try:
        asyncio.run(serve(settings))
    except StorageError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
