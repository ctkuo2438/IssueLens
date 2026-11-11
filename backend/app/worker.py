"""Run queued analyses in a process independent of the HTTP server."""

import asyncio
from functools import partial
import signal

from backend.app.agent import checkpoint_signature
from backend.app.config import Settings
from backend.app.jobs import Job, JobManager
from backend.app.models import TERMINAL_STATUSES
from backend.app.storage import POLL_SECONDS, PostgresAnalysisStore, StorageError


class AnalysisWorker:
    def __init__(self, settings: Settings) -> None:
        self.store = PostgresAnalysisStore(settings.database_url)
        self.runner = JobManager(settings)
        self.signature = checkpoint_signature(settings)

    async def _publish(self, active: dict[str, Job]) -> None:
        for job_id, job in list(active.items()):
            if job.task is not None and job.task.done() and not job.task.cancelled():
                job.task.result()
            if job.snapshot.status in TERMINAL_STATUSES:
                await self.store.save_running(job.snapshot, trace=job.trace)
                del active[job_id]

    async def run(self, stop: asyncio.Event) -> None:
        await self.store.open(worker=True)
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
                            job, checkpoint=claimed.checkpoint,
                            save_checkpoint=partial(self.store.save_running, job.snapshot, trace=job.trace),
                        )

                    try:
                        await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
                    except TimeoutError:
                        pass
            finally:
                await asyncio.gather(*(self.runner.cancel(job, suspend=True) for job in active.values()), return_exceptions=True)
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
