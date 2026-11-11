"""Verify Agent replay and Worker replacement with fictional provider responses."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import signal
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from openai.types.responses import ResponseReasoningItem
import psycopg

from backend.app.agent import AgentRun, checkpoint_signature
from backend.app.checkpoint import AgentCheckpoint
from backend.app.config import ROOT
from backend.app.models import JobSnapshot, TERMINAL_STATUSES
from backend.app.worker import serve
from test_mvp import (
    SETTINGS, TARGET, OfflineEncoding, final_content, github_response, simulated_response, tool_call,
)
from test_persistence import api_client


REPLAY_TOKEN = "opaque-fixture-reasoning"


class AgentCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_repair_keeps_its_evidence_feedback_and_used_attempt(self):
        settings = replace(SETTINGS, max_iterations=4)
        saved = None
        requests = []
        github = httpx.MockTransport(github_response)

        async def persist(checkpoint):
            nonlocal saved
            saved = checkpoint.model_dump_json()

        async def respond(**request):
            requests.append(request)
            if len(requests) == 1:
                return simulated_response([tool_call("get_issue")])
            if len(requests) == 2:
                return simulated_response(content=final_content("S999"))
            if len(requests) == 3:
                raise asyncio.CancelledError
            return simulated_response(content=final_content())

        client = AsyncMock()
        client.__aenter__.return_value.responses.create.side_effect = respond
        initial = AgentCheckpoint.start(checkpoint_signature(settings))
        with (
            patch("backend.app.agent.AsyncOpenAI", return_value=client),
            patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
            patch.object(AgentRun, "required_lookup", return_value=None),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await AgentRun(TARGET, settings, lambda *_: None, github_transport=github,
                               checkpoint=initial, save_checkpoint=persist).run()
            recovered = AgentCheckpoint.model_validate_json(saved)
            self.assertEqual(recovered.in_flight, "model")
            self.assertEqual(recovered.result.usage.model_requests, 3)
            self.assertFalse(recovered.result.context_builds[-1].sent)
            result = await AgentRun(TARGET, settings, lambda *_: None, github_transport=github,
                                    checkpoint=recovered, save_checkpoint=persist).run()

        self.assertIsNotNone(result.content)
        self.assertEqual(result.usage.model_requests, 4)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(result.usage.total_tokens, 75)
        self.assertEqual(len(result.rejected_answers), 1)
        self.assertEqual(requests[2]["input"], requests[3]["input"])
        self.assertEqual(requests[3]["tool_choice"], "none")
        self.assertIn(recovered.repair_feedback, requests[3]["instructions"])
        self.assertTrue(AgentCheckpoint.model_validate_json(saved).finished)


def fixture_settings(database_url):
    return replace(SETTINGS, database_url=database_url, context_tokens=12_000, reasoning_effort="medium")


async def run_fixture_worker(directory: Path, generation: str, scenario: str) -> None:
    """Use the normal Worker and SDK with local responses and a controlled interruption."""
    trace = directory / f"{generation}.jsonl"

    def record(entry):
        with trace.open("a") as output:
            output.write(json.dumps(entry) + "\n")

    def model_response(request):
        body = json.loads(request.content)
        number = int(re.search(r"This is model request (\d+) of", body["instructions"])[1])
        record({"kind": "model", "number": number, "input": body["input"]})
        if number == 1:
            output = [ResponseReasoningItem(
                id="rs_fixture", type="reasoning", summary=[], encrypted_content=REPLAY_TOKEN,
            ).to_dict(), tool_call("get_issue", call_id="call_issue").to_dict()]
        elif number == 2:
            output = [tool_call("get_repo_labels", call_id="call_labels").to_dict()]
        elif number in (3, 4):
            query = "export" if number == 3 else "windows"
            output = [tool_call("search_issues", json.dumps({"query": query}), f"call_search_{number}").to_dict()]
        else:
            if number != 5:
                raise AssertionError("Unexpected fixture model request.")
            output = [{
                "id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(final_content()), "annotations": []}],
            }]
        return httpx.Response(200, json={
            "id": f"resp_fixture_{number}", "object": "response", "created_at": 0,
            "model": "gpt-5.6", "status": "completed", "output": output,
            "incomplete_details": None, "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        })

    async def github_fixture(request):
        path = request.url.path
        record({"kind": "tool", "path": path, "at": time.time()})
        if path.endswith("/labels"):
            if generation == "first":
                (directory / "labels-started").touch()
                if scenario == "retry_wait":
                    return httpx.Response(503, headers={"Retry-After": "4"})
                await asyncio.Future()
            return httpx.Response(200, json=[])
        if path == "/search/issues":
            return httpx.Response(200, json={"items": [], "total_count": 0, "incomplete_results": False})
        return github_response(request)

    def model_client(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(model_response)))

    def agent_run(*args, **kwargs):
        return AgentRun(*args, **kwargs, github_transport=httpx.MockTransport(github_fixture))

    with (
        patch("backend.app.jobs.AgentRun", side_effect=agent_run),
        patch("backend.app.agent.AsyncOpenAI", side_effect=model_client),
    ):
        await serve(fixture_settings(os.environ["ISSUELENS_TEST_DATABASE_URL"]))


@unittest.skipUnless(os.getenv("ISSUELENS_TEST_DATABASE_URL"), "Requires a real PostgreSQL test database.")
class WorkerCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_analysis_completes_after_worker_process_replacement(self):
        database_url = os.environ["ISSUELENS_TEST_DATABASE_URL"]
        if not urlsplit(database_url).path.endswith("_test"):
            self.skipTest("Use a dedicated database whose name ends in _test.")
        for stop_signal in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(stop_signal=stop_signal.name), TemporaryDirectory() as temporary:
                await self.check_restart(database_url, Path(temporary), stop_signal)

    async def test_replacement_worker_waits_until_the_saved_retry_time(self):
        database_url = os.environ["ISSUELENS_TEST_DATABASE_URL"]
        if not urlsplit(database_url).path.endswith("_test"):
            self.skipTest("Use a dedicated database whose name ends in _test.")
        with TemporaryDirectory() as temporary:
            await self.check_restart(database_url, Path(temporary), signal.SIGTERM, scenario="retry_wait")

    async def check_restart(self, database_url, directory, stop_signal, *, scenario="tool_interruption"):
        processes = []
        job_id = None

        async def start_worker(generation):
            with (directory / f"{generation}.log").open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c",
                    "import asyncio, sys; sys.path.insert(0, 'tests'); "
                    "from pathlib import Path; from test_checkpoint import run_fixture_worker; "
                    "asyncio.run(run_fixture_worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))",
                    str(directory), generation, scenario, cwd=ROOT, stdout=log, stderr=asyncio.subprocess.STDOUT,
                )
            processes.append(process)
            return process

        async def get_checkpoint(connection):
            cursor = await connection.execute("SELECT checkpoint FROM issuelens_analyses WHERE job_id = %s", (job_id,))
            return (await cursor.fetchone())[0]

        settings = fixture_settings(database_url)
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as connection:
            try:
                async with api_client(settings) as api:
                    created = await api.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                    self.assertEqual(created.status_code, 202)
                    job_id = created.json()["job_id"]
                    path = f"/api/v1/analyses/{job_id}"
                    first = await start_worker("first")
                    async with asyncio.timeout(12):
                        while not (directory / "labels-started").exists():
                            self.assertIsNone(first.returncode, (directory / "first.log").read_text())
                            await asyncio.sleep(0.02)

                        while True:
                            saved = AgentCheckpoint.model_validate_json(await get_checkpoint(connection))
                            if scenario != "retry_wait" or saved.retry is not None:
                                break
                            self.assertIsNone(first.returncode, (directory / "first.log").read_text())
                            await asyncio.sleep(0.02)

                    self.assertEqual(saved.in_flight, None if scenario == "retry_wait" else "tool")
                    self.assertEqual(saved.result.usage.tool_calls, 2)
                    self.assertEqual(saved.pending_tools.calls[0].call_id, "call_labels")
                    self.assertEqual(saved.result.usage.model_requests, 2)
                    self.assertEqual(saved.result.sources[0].id, "S1")
                    before = (await api.get(path)).json()
                    self.assertEqual(before["status"], "running")
                    self.assertEqual(set(before), set(JobSnapshot.model_fields))
                    self.assertNotIn(REPLAY_TOKEN, json.dumps(before))
                    trace_path = f"{path}/trace"
                    trace_response = await api.get(trace_path)
                    self.assertEqual(trace_response.status_code, 200)
                    before_execution = trace_response.json()
                    self.assertEqual((before_execution["job_id"], before_execution["status"]), (job_id, "running"))
                    first_attempts = before_execution["attempts"]
                    self.assertEqual([item["kind"] for item in first_attempts], ["model", "tool", "model", "tool"])
                    labels_attempt = first_attempts[-1]
                    self.assertEqual(labels_attempt["name"], "get_repo_labels")
                    self.assertEqual(labels_attempt["outcome"], "failed" if scenario == "retry_wait" else "running")
                    if scenario == "retry_wait":
                        self.assertEqual(labels_attempt["id"], saved.retry.attempt_id)
                        self.assertEqual(labels_attempt["retry_scheduled_for"], saved.retry.model_dump(mode="json")["not_before"])
                        self.assertGreaterEqual(labels_attempt["duration_ms"], 0)
                    else:
                        self.assertIsNone(labels_attempt["duration_ms"])

                    first.send_signal(stop_signal)
                    await asyncio.wait_for(first.wait(), timeout=6)
                    self.assertEqual((await api.get(path)).json(), before)
                    self.assertEqual((await api.get(trace_path)).json(), before_execution)
                    replacement = await start_worker("replacement")
                    self.assertNotEqual(first.pid, replacement.pid)
                    async with asyncio.timeout(12):
                        while True:
                            current = (await api.get(path)).json()
                            if current["status"] in TERMINAL_STATUSES:
                                break
                            self.assertIsNone(replacement.returncode, (directory / "replacement.log").read_text())
                            await asyncio.sleep(0.03)
                    self.assertEqual(current["status"], "completed", current)
                    self.assertEqual(current["job_id"], job_id)
                    self.assertEqual(current["result"]["sources"][0], saved.result.sources[0].model_dump(mode="json"))
                    self.assertEqual(current["result"]["usage"]["model_requests"], 5)
                    self.assertEqual(current["result"]["usage"]["tool_calls"], 5)
                    self.assertEqual(current["result"]["usage"]["total_tokens"], 125)
                    self.assertEqual(current["events"][:len(before["events"])], before["events"])
                    self.assertEqual([event["id"] for event in current["events"]], list(range(1, len(current["events"]) + 1)))
                    replay = await api.get(f"{path}/events", headers={"Last-Event-ID": str(before["events"][-1]["id"])})
                    self.assertIn("Resumed from a checkpoint", replay.text)
                    self.assertIn("event: done", replay.text)
                    self.assertNotIn(REPLAY_TOKEN, replay.text)
                    self.assertIsNone(await get_checkpoint(connection))
                    execution = (await api.get(trace_path)).json()
                    self.assertEqual((execution["job_id"], execution["status"], execution["stop_reason"]), (
                        job_id, "completed", "final_answer",
                    ))
                    attempts = execution["attempts"]
                    self.assertEqual([item["id"] for item in attempts], list(range(1, 11)))
                    for kind in ("model", "tool"):
                        self.assertEqual([item["attempt_number"] for item in attempts if item["kind"] == kind], list(range(1, 6)))
                    self.assertEqual(attempts[:3], first_attempts[:3])
                    if scenario == "retry_wait":
                        self.assertEqual(attempts[3], labels_attempt)
                        self.assertTrue(attempts[4]["is_retry"])
                        self.assertEqual(attempts[4]["retry_of"], labels_attempt["id"])
                    else:
                        self.assertEqual(attempts[3]["outcome"], "interrupted")
                        self.assertEqual(attempts[3]["stop_reason"], "worker_interrupted")
                        self.assertEqual(attempts[3]["started_at"], labels_attempt["started_at"])
                        self.assertIsNone(attempts[3]["duration_ms"])
                    self.assertEqual(attempts[4]["name"], "get_repo_labels")
                    for item in attempts[4:]:
                        self.assertEqual(item["outcome"], "succeeded")
                        self.assertGreaterEqual(item["duration_ms"], 0)
                    for private in (REPLAY_TOKEN, "call_labels", TARGET.url, settings.openai_api_key):
                        self.assertNotIn(private, json.dumps(execution))

                replacement.terminate()
                await asyncio.wait_for(replacement.wait(), timeout=6)
                async with api_client(settings) as restarted_api:
                    self.assertEqual((await restarted_api.get(trace_path)).json(), execution)

                before_trace, after_trace = [
                    [json.loads(line) for line in (directory / f"{generation}.jsonl").read_text().splitlines()]
                    for generation in ("first", "replacement")
                ]
                self.assertEqual([entry["number"] for entry in before_trace if entry["kind"] == "model"], [1, 2])
                new_requests = [entry for entry in after_trace if entry["kind"] == "model"]
                self.assertEqual([entry["number"] for entry in new_requests], [3, 4, 5])
                self.assertEqual(new_requests[0]["input"][:len(saved.history)], saved.history)
                self.assertEqual(new_requests[0]["input"][-1]["call_id"], "call_labels")
                self.assertEqual([entry["path"] for entry in after_trace if entry["kind"] == "tool"], [
                    f"{TARGET.api_path}/labels", "/search/issues", "/search/issues",
                ])
                if scenario == "retry_wait":
                    retry_attempt = next(entry for entry in after_trace if entry["kind"] == "tool")
                    self.assertGreaterEqual(retry_attempt["at"], saved.retry.not_before.timestamp())
                    self.assertTrue(any("retry for get_repo_labels succeeded" in event["message"] for event in current["events"]))
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=6)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                if job_id is not None:
                    await connection.execute("DELETE FROM issuelens_analyses WHERE job_id = %s", (job_id,))
