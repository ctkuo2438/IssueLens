"""The job runner's outcomes for runs that are stopped, cancelled or cannot save, without provider requests."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.app import lifecycle
from backend.app.agent import checkpoint_signature
from backend.app.checkpoint import AgentCheckpoint
from backend.app.github import GitHubToolState
from backend.app.jobs import Job, JobManager
from backend.app.storage import StorageError
from test_mvp import SETTINGS, TARGET, OfflineEncoding, github_response


@contextmanager
def offline(model):
    """Simulate GitHub, the tokenizer and OpenAI, whose requests `model` answers."""
    sdk = AsyncMock()
    sdk.__aenter__.return_value.responses.create = AsyncMock(side_effect=model)
    github = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(github_response))
    with (
        patch("backend.app.agent.httpx.AsyncClient", return_value=github),
        patch("backend.app.agent.AsyncOpenAI", return_value=sdk),
        patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
    ):
        yield


class JobRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_checkpoint_write_is_raised_instead_of_finishing_the_job(self):
        # The Worker reconnects and resumes from the last checkpoint, so the job must stay unfinished.
        async def failing_save(state):
            raise StorageError("The analysis database is unavailable.")

        manager = JobManager(SETTINGS)
        job = Job(lifecycle.queued_snapshot(TARGET))
        with offline(AsyncMock()):
            manager.schedule(job, checkpoint=AgentCheckpoint.start(checkpoint_signature(SETTINGS)), save_checkpoint=failing_save)
            assert job.task is not None
            with self.assertRaises(StorageError):
                await job.task
        self.assertEqual(job.snapshot.status, "running")
        self.assertIsNone(job.snapshot.result)

    async def test_a_job_cancelled_while_waiting_for_a_slot_finishes_without_a_result(self):
        release = asyncio.Event()

        async def held_model(**kwargs):
            await release.wait()

        manager = JobManager(replace(SETTINGS, concurrent_jobs=1))
        with offline(held_model):
            first = await manager.create(TARGET)
            second = await manager.create(TARGET)
            # One loop turn starts both tasks: the first takes the only slot, the second waits for it.
            await asyncio.sleep(0)
            await manager.cancel(second)
            self.assertEqual(second.snapshot.status, "cancelled")
            self.assertIsNone(second.snapshot.result)
            self.assertEqual(second.trace.stop_reason, "job_cancelled")
            self.assertEqual([event.message for event in second.snapshot.events][-1], "Analysis cancelled.")
            await manager.close()
        self.assertEqual(first.snapshot.status, "cancelled")

    async def test_a_resumed_job_without_runtime_left_keeps_its_evidence(self):
        checkpoint = AgentCheckpoint.start(checkpoint_signature(SETTINGS))
        checkpoint.started_at -= timedelta(seconds=SETTINGS.runtime_seconds + 1)
        checkpoint.result.usage.model_requests = 1
        checkpoint.tools = GitHubToolState(limitations=["Saved tool limitation."])
        manager = JobManager(SETTINGS)
        job = Job(lifecycle.queued_snapshot(TARGET))
        manager.schedule(job, checkpoint=checkpoint)
        assert job.task is not None
        await job.task
        self.assertEqual(job.snapshot.status, "partial")
        self.assertEqual(job.trace.stop_reason, "job_runtime_limit")
        assert job.snapshot.result is not None
        self.assertEqual(job.snapshot.result.limitations, [
            "Saved tool limitation.", "The job runtime limit was reached. Further investigation was not completed.",
        ])
        # A resumed run that had already asked the model does not announce a fresh start.
        self.assertNotIn("Reading evidence and investigating the issue.", [event.message for event in job.snapshot.events])


if __name__ == "__main__":
    unittest.main()
