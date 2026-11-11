"""Offline lifecycle checks and opt-in PostgreSQL queue and Worker checks."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
import psycopg
from psycopg.types.json import Jsonb

from backend.app import config
from backend.app.agent import checkpoint_signature
from backend.app.config import Settings
from backend.app.jobs import JobManager, QueueFullError
from backend.app.main import create_app
from backend.app.models import JobSnapshot, ProgressEvent, TERMINAL_STATUSES
from backend.app.storage import INTERRUPTED, PostgresAnalysisStore, StorageError, interrupted_snapshot
from backend.app.worker import AnalysisWorker
from test_mvp import SETTINGS, TARGET, offline_result


class FakeStore:
    """JSON copies shared across fake backend lifetimes; this is not PostgreSQL."""

    def __init__(self, records=None):
        self.records = records if records is not None else {}
        self.fail_statuses = set()
        self.fail_reads = False
        self.block_status = None
        self.write_started = asyncio.Event()
        self.allow_write = asyncio.Event()

    async def open(self):
        for job_id, payload in list(self.records.items()):
            snapshot = JobSnapshot.model_validate_json(payload)
            if snapshot.status in {"queued", "running"}:
                self.records[job_id] = interrupted_snapshot(snapshot).model_dump_json()

    async def save(self, snapshot):
        if snapshot.status in self.fail_statuses:
            raise StorageError("SENSITIVE_DATABASE_CANARY")
        if snapshot.status == self.block_status:
            self.write_started.set()
            await self.allow_write.wait()
        self.records[snapshot.job_id] = snapshot.model_dump_json()

    async def get(self, job_id):
        if self.fail_reads:
            raise StorageError("SENSITIVE_DATABASE_CANARY")
        payload = self.records.get(job_id)
        return JobSnapshot.model_validate_json(payload) if payload else None

    async def close(self):
        pass


def saved_snapshot(status="completed"):
    return JobSnapshot(
        job_id=uuid4().hex, issue_url=TARGET.url, status=status,
        created_at=datetime.now(UTC), result=offline_result(),
        events=[ProgressEvent(id=1, kind="status", message="Offline fixture record.")],
    )


@asynccontextmanager
async def api_client(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client


class PersistenceConfigurationTests(unittest.TestCase):
    def test_database_setting_is_optional_and_never_appears_in_settings_repr(self):
        for value in ("", "postgresql://user:SENSITIVE_DATABASE_CANARY@localhost/project"):
            with patch.dict(os.environ, {"DATABASE_URL": value}, clear=True), patch.object(config, "load_dotenv"):
                settings = Settings.from_env()
            self.assertEqual(settings.database_url, value)
            self.assertNotIn("SENSITIVE_DATABASE_CANARY", repr(settings))
            self.assertEqual((settings.max_iterations, settings.max_tool_calls, settings.context_tokens), (8, 15, 12000))

    def test_other_database_schemes_fail_without_echoing_credentials(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://SENSITIVE_DATABASE_CANARY"}, clear=True), patch.object(config, "load_dotenv"):
            with self.assertRaises(ValueError) as error:
                Settings.from_env()
        self.assertNotIn("SENSITIVE_DATABASE_CANARY", str(error.exception))


class PersistenceAPITests(unittest.TestCase):
    def test_results_and_event_replay_survive_a_new_backend_without_a_model_key(self):
        records = {}
        run = AsyncMock(return_value=offline_result())
        with patch("backend.app.jobs.AgentRun.run", new=run):
            with TestClient(create_app(SETTINGS, store=FakeStore(records))) as client:
                job_id = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                self.assertIn("event: done", client.get(f"/api/v1/analyses/{job_id}/events").text)
                original = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(original["status"], "completed")
            with TestClient(create_app(Settings(), store=FakeStore(records))) as restarted:
                self.assertEqual(restarted.get(f"/api/v1/analyses/{job_id}").json(), original)
                replay = restarted.get(f"/api/v1/analyses/{job_id}/events", headers={"Last-Event-ID": "2"})
                self.assertNotIn("id: 1\n", replay.text)
                self.assertIn("event: done", replay.text)
                self.assertEqual(restarted.get("/api/v1/health").json()["storage"], "postgresql")
                self.assertFalse(restarted.get("/api/v1/health").json()["openai_configured"])
                self.assertEqual(restarted.delete(f"/api/v1/analyses/{job_id}").json(), original)
        self.assertEqual(run.await_count, 1)
        self.assertNotIn(SETTINGS.openai_api_key, json.dumps(records))

    def test_database_rejection_prevents_job_creation_and_paid_work(self):
        store = FakeStore()
        store.fail_statuses = {"queued"}
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock()) as run:
            with TestClient(create_app(SETTINGS, store=store)) as client:
                response = client.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("SENSITIVE_DATABASE_CANARY", response.text)
                self.assertEqual(client.app.state.manager.jobs, {})
        run.assert_not_awaited()

    def test_unavailable_reads_are_503_instead_of_missing_records(self):
        store = FakeStore()
        store.fail_reads = True
        with TestClient(create_app(Settings(), store=store)) as client:
            for suffix in ("", "/events"):
                response = client.get(f"/api/v1/analyses/{uuid4().hex}{suffix}")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("SENSITIVE_DATABASE_CANARY", response.text)

    def test_cache_expiry_and_eviction_do_not_delete_saved_results(self):
        store = FakeStore()
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=offline_result())):
            with TestClient(create_app(replace(SETTINGS, max_jobs=1, job_ttl_seconds=0), store=store)) as client:
                first = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                client.get(f"/api/v1/analyses/{first}/events")
                second = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                client.get(f"/api/v1/analyses/{second}/events")
                self.assertEqual(client.get(f"/api/v1/analyses/{first}").json()["status"], "completed")
                self.assertEqual(len(store.records), 2)
                self.assertLessEqual(len(client.app.state.manager.jobs), 1)

    def test_failed_final_write_is_not_advertised_as_durable_completion(self):
        records = {}
        store = FakeStore(records)
        store.fail_statuses = {"completed"}
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=offline_result())):
            with TestClient(create_app(SETTINGS, store=store)) as client:
                job_id = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                stream = client.get(f"/api/v1/analyses/{job_id}/events").text
                result = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(result["status"], "partial")
                self.assertIsNotNone(result["result"]["content"])
                self.assertIn("available only", result["error"])
                self.assertNotIn('"status": "completed"', stream)
            with TestClient(create_app(Settings(), store=FakeStore(records))) as restarted:
                restored = restarted.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(restored["status"], "cancelled")
                self.assertEqual(restored["error"], INTERRUPTED)

    def test_failed_running_write_never_starts_the_agent(self):
        store = FakeStore()
        store.fail_statuses = {"running"}
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock()) as run:
            with TestClient(create_app(SETTINGS, store=store)) as client:
                job_id = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                client.get(f"/api/v1/analyses/{job_id}/events")
                result = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(result["status"], "failed")
                self.assertIn("could not start", result["error"])
        run.assert_not_awaited()

    def test_timeout_and_cancellation_records_survive_restart(self):
        async def wait(run):
            run.result = offline_result()
            await asyncio.sleep(60)

        for action in ("timeout", "cancel"):
            with self.subTest(action=action):
                records = {}
                settings = replace(SETTINGS, runtime_seconds=0.01) if action == "timeout" else SETTINGS
                with patch("backend.app.jobs.AgentRun.run", new=wait):
                    with TestClient(create_app(settings, store=FakeStore(records))) as client:
                        job_id = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                        if action == "cancel":
                            client.delete(f"/api/v1/analyses/{job_id}")
                        else:
                            client.get(f"/api/v1/analyses/{job_id}/events")
                        original = client.get(f"/api/v1/analyses/{job_id}").json()
                with TestClient(create_app(Settings(), store=FakeStore(records))) as restarted:
                    self.assertEqual(restarted.get(f"/api/v1/analyses/{job_id}").json(), original)
                self.assertEqual(original["status"], "partial" if action == "timeout" else "cancelled")


class PersistenceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_records_become_terminal_once_without_discarding_evidence(self):
        for status in ("queued", "running"):
            original = saved_snapshot(status)
            records = {original.job_id: original.model_dump_json()}
            store = FakeStore(records)
            await store.open()
            restored = await store.get(original.job_id)
            self.assertEqual(restored.status, "cancelled")
            self.assertEqual(restored.result, original.result)
            self.assertEqual(restored.events[0], original.events[0])
            self.assertEqual([event.id for event in restored.events], [1, 2, 3])
            payload = records[original.job_id]
            await FakeStore(records).open()
            self.assertEqual(records[original.job_id], payload)

    async def test_completion_is_hidden_until_commit_and_cancellation_waits_for_that_commit(self):
        store = FakeStore()
        store.block_status = "completed"
        manager = JobManager(SETTINGS, store)
        await manager.start()
        try:
            with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=offline_result())):
                job = await manager.create(TARGET)
                await asyncio.wait_for(store.write_started.wait(), timeout=2)
                self.assertEqual(job.snapshot.status, "running")
                self.assertFalse(any(event.message == "Analysis completed." for event in job.snapshot.events))
                cancelling = asyncio.create_task(manager.cancel(job))
                await asyncio.sleep(0)
                self.assertFalse(cancelling.done())
                store.allow_write.set()
                await cancelling
                self.assertEqual(job.snapshot.status, "completed")
                self.assertEqual((await store.get(job.snapshot.job_id)), job.snapshot)
        finally:
            store.allow_write.set()
            await manager.close()

    async def test_concurrent_creates_cannot_exceed_capacity_while_a_save_is_pending(self):
        store = FakeStore()
        store.block_status = "queued"
        manager = JobManager(replace(SETTINGS, max_jobs=1), store)
        await manager.start()
        try:
            async def wait(run):
                await asyncio.sleep(60)

            with patch("backend.app.jobs.AgentRun.run", new=wait):
                first = asyncio.create_task(manager.create(TARGET))
                await asyncio.wait_for(store.write_started.wait(), timeout=2)
                second = asyncio.create_task(manager.create(TARGET))
                store.allow_write.set()
                await first
                with self.assertRaises(QueueFullError):
                    await second
                self.assertEqual(len(manager.jobs), 1)
        finally:
            store.allow_write.set()
            await manager.close()


@unittest.skipUnless(os.getenv("ISSUELENS_TEST_DATABASE_URL"), "Real PostgreSQL checks require ISSUELENS_TEST_DATABASE_URL.")
class PostgresIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database_url = os.environ["ISSUELENS_TEST_DATABASE_URL"]
        if not urlsplit(self.database_url).path.endswith("_test"):
            self.skipTest("Use a dedicated database whose name ends in _test.")
        self.created_ids = []
        self.store = PostgresAnalysisStore(self.database_url)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        if self.created_ids:
            async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
                await connection.execute("DELETE FROM issuelens_analyses WHERE job_id = ANY(%s)", (self.created_ids,))

    async def test_jsonb_roundtrip_survives_a_new_database_connection(self):
        snapshot = saved_snapshot("partial")
        snapshot.result.sources[0].excerpt = "A multiline source.\nThe reporter's exact text includes naïve input."
        self.created_ids.append(snapshot.job_id)
        await self.store.save(snapshot)
        await self.store.close()
        self.store = PostgresAnalysisStore(self.database_url)
        await self.store.open()
        self.assertEqual(await self.store.get(snapshot.job_id), snapshot)

    async def test_only_a_replacement_worker_marks_abandoned_running_jobs_interrupted(self):
        owner = PostgresAnalysisStore(self.database_url)
        replacement = PostgresAnalysisStore(self.database_url)
        running, queued = saved_snapshot("running"), saved_snapshot("queued")
        self.created_ids.extend([running.job_id, queued.job_id])
        try:
            await owner.open(worker=True)
            await owner.save(running)
            await self.store.save(queued)
            with self.assertRaisesRegex(StorageError, "Another IssueLens Worker"):
                await replacement.open(worker=True)
            await replacement.open()
            self.assertEqual(await replacement.get(running.job_id), running)
            await replacement.close()
            await owner.close()

            await replacement.open(worker=True)
            restored = await self.store.get(running.job_id)
            self.assertEqual((restored.status, restored.error), ("cancelled", INTERRUPTED))
            self.assertEqual(restored.result, running.result)
            self.assertEqual(restored.events[0], running.events[0])
            self.assertEqual([event.id for event in restored.events], [1, 2, 3])
            self.assertEqual(await self.store.get(queued.job_id), queued)
        finally:
            await owner.close()
            await replacement.close()

    async def test_updating_one_snapshot_preserves_other_analyses(self):
        first, second = saved_snapshot(), saved_snapshot()
        self.created_ids.extend([first.job_id, second.job_id])
        await self.store.save(first)
        await self.store.save(second)
        first.result.content.draft_reply = "An updated fixture draft."
        first.events.append(ProgressEvent(id=2, kind="warning", message="A fixture observation."))
        await self.store.save(first)
        self.assertEqual(await self.store.get(first.job_id), first)
        self.assertEqual(await self.store.get(second.job_id), second)

    async def test_untrusted_identifiers_are_parameters_and_missing_records_are_none(self):
        snapshot = saved_snapshot()
        self.created_ids.append(snapshot.job_id)
        await self.store.save(snapshot)
        self.assertIsNone(await self.store.get("'; DROP TABLE issuelens_analyses; --"))
        self.assertEqual(await self.store.get(snapshot.job_id), snapshot)

    async def test_invalid_stored_payload_has_a_sanitized_failure(self):
        job_id = uuid4().hex
        self.created_ids.append(job_id)
        async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
            await connection.execute(
                "INSERT INTO issuelens_analyses (job_id, snapshot) VALUES (%s, %s)",
                (job_id, Jsonb({"job_id": job_id, "private": "SENSITIVE_DATABASE_CANARY"})),
            )
        with self.assertRaises(StorageError) as error:
            await self.store.get(job_id)
        self.assertNotIn("SENSITIVE_DATABASE_CANARY", str(error.exception))

    async def test_actual_postgres_restores_api_results_and_sse_without_new_agent_work(self):
        snapshot = saved_snapshot()
        self.created_ids.append(snapshot.job_id)
        await self.store.save(snapshot)
        path = f"/api/v1/analyses/{snapshot.job_id}"
        async with api_client(Settings(database_url=self.database_url)) as client:
            self.assertEqual((await client.get(path)).json(), snapshot.model_dump(mode="json"))
            self.assertIn("event: done", (await client.get(f"{path}/events")).text)
            self.assertEqual((await client.get("/api/v1/health")).json()["storage"], "postgresql")
            trace = await client.get(f"{path}/trace")
            self.assertEqual(trace.status_code, 200)
            self.assertEqual(trace.json(), {
                "job_id": snapshot.job_id, "status": snapshot.status, "stop_reason": None, "attempts": [],
            })

    async def test_separate_api_connections_share_one_queue_capacity(self):
        first, second = saved_snapshot("queued"), saved_snapshot("queued")
        self.created_ids.extend([first.job_id, second.job_id])
        other = PostgresAnalysisStore(self.database_url)
        try:
            await other.open()
            accepted = await asyncio.gather(self.store.enqueue(first, 1), other.enqueue(second, 1))
            self.assertEqual(sorted(accepted), [False, True])
        finally:
            await other.close()

    async def test_replacement_respects_finished_checkpoint_expired_budget_and_durable_cancel(self):
        settings = replace(SETTINGS, database_url=self.database_url, concurrent_jobs=1)
        signature = checkpoint_signature(settings)
        owner = PostgresAnalysisStore(self.database_url)
        stop = asyncio.Event()
        async with api_client(settings) as client:
            paths = {status: await self.submit(client) for status in ("completed", "partial", "cancelled")}
            await owner.open(worker=True)
            try:
                active_ids = []
                for status, path in paths.items():
                    claimed = await owner.claim(active_ids, signature)
                    self.assertTrue(path.endswith(claimed.snapshot.job_id))
                    active_ids.append(claimed.snapshot.job_id)
                    result = offline_result()
                    if status != "completed":
                        result.content = None
                    state = claimed.checkpoint.model_copy(update={
                        "started_at": datetime.now(UTC) - timedelta(seconds=settings.runtime_seconds + 1),
                        "finished": status == "completed",
                        "result": result,
                    })
                    await owner.save_running(claimed.snapshot, state)
            finally:
                await owner.close()
            self.assertEqual((await client.delete(paths["cancelled"])).status_code, 202)

            with patch("backend.app.jobs.AgentRun.run", new=AsyncMock()) as run:
                worker = asyncio.create_task(AnalysisWorker(settings).run(stop))
                try:
                    for status, path in paths.items():
                        final = await self.wait_for_snapshot(path, lambda item: item.status in TERMINAL_STATUSES)
                        self.assertEqual(final.status, status)
                        self.assertEqual(final.result.sources, offline_result().sources)
                        self.assertEqual(final.result.content is not None, status == "completed")
                    run.assert_not_awaited()
                finally:
                    stop.set()
                    await worker

    async def submit(self, client, issue_url=TARGET.url):
        response = await client.post("/api/v1/analyses", json={"issue_url": issue_url})
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_id"]
        self.created_ids.append(job_id)
        return f"/api/v1/analyses/{job_id}"

    async def wait_for_snapshot(self, path, condition):
        async with asyncio.timeout(5):
            while True:
                snapshot = await self.store.get(path.rsplit("/", 1)[-1])
                if snapshot and condition(snapshot):
                    return snapshot
                await asyncio.sleep(0.02)

    async def test_worker_keeps_running_and_replays_progress_across_api_restart(self):
        settings = replace(SETTINGS, database_url=self.database_url, concurrent_jobs=1)
        stop, advance, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def controlled(run):
            calls.append(run.target.number)
            if run.target.number == TARGET.number:
                run.emit("status", "Fixture started before the API stopped.")
                await run.save_checkpoint(run.checkpoint)
                await advance.wait()
                run.emit("status", "Fixture advanced while the API was offline.")
                await run.save_checkpoint(run.checkpoint)
                await finish.wait()
            return offline_result()

        with patch("backend.app.jobs.AgentRun.run", new=controlled):
            worker = asyncio.create_task(AnalysisWorker(settings).run(stop))
            try:
                async with api_client(settings) as first:
                    active = await self.submit(first)
                    pending = await self.submit(first, "https://github.com/example/project/issues/43")
                    before = await self.wait_for_snapshot(active, lambda item: len(item.events) >= 3)
                    self.assertEqual(before.status, "running")
                    self.assertEqual((await first.get(pending)).json()["status"], "queued")
                # The API's lifespan has ended and its database connection is closed.
                advance.set()
                offline = await self.wait_for_snapshot(active, lambda item: len(item.events) > len(before.events))
                self.assertEqual(offline.status, "running")
                async with api_client(settings) as restarted:
                    self.assertEqual((await restarted.get(active)).json(), offline.model_dump(mode="json"))
                    self.assertEqual((await restarted.get(pending)).json()["status"], "queued")
                    finish.set()
                    async with asyncio.timeout(5):
                        replay = await restarted.get(f"{active}/events", headers={"Last-Event-ID": str(before.events[-1].id)})
                    self.assertNotIn(f"id: {before.events[-1].id}\n", replay.text)
                    self.assertIn("Fixture advanced while the API was offline.", replay.text)
                    self.assertIn("event: done", replay.text)
                    self.assertEqual((await restarted.get(active)).json()["status"], "completed")
                    await self.wait_for_snapshot(pending, lambda item: item.status == "completed")
                self.assertEqual(calls, [42, 43])
            finally:
                stop.set()
                await worker

    async def test_cancel_queued_and_running_analyses_through_the_api(self):
        settings = replace(SETTINGS, database_url=self.database_url, concurrent_jobs=1)
        stop = asyncio.Event()
        calls = []

        async def controlled(run):
            calls.append(run.target.number)
            run.result = offline_result()
            run.emit("status", "Fixture waiting for cancellation.")
            await run.save_checkpoint(run.checkpoint.model_copy(update={"result": run.result}))
            await asyncio.Future()

        with patch("backend.app.jobs.AgentRun.run", new=controlled):
            async with api_client(settings) as client:
                active = await self.submit(client)
                queued = await self.submit(client, "https://github.com/example/project/issues/43")
                worker = asyncio.create_task(AnalysisWorker(settings).run(stop))
                try:
                    await self.wait_for_snapshot(active, lambda item: len(item.events) >= 3)
                    cancelled = await client.delete(queued)
                    self.assertEqual((cancelled.status_code, cancelled.json()["status"]), (200, "cancelled"))
                    accepted = await client.delete(active)
                    self.assertEqual((accepted.status_code, accepted.json()["status"]), (202, "running"))
                    final = await self.wait_for_snapshot(active, lambda item: item.status == "cancelled")
                    self.assertEqual(final.result.sources, offline_result().sources)
                    self.assertEqual((await client.delete(active)).json(), final.model_dump(mode="json"))
                    self.assertEqual(calls, [42])
                finally:
                    stop.set()
                    await worker


if __name__ == "__main__":
    unittest.main()
