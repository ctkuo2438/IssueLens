"""Exercise temporary failure handling without making provider requests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
from openai import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError

from backend.app.agent import AgentRun, checkpoint_signature
from backend.app.checkpoint import AgentCheckpoint
from backend.app.github import github_rate_limit_delay
from backend.app.main import create_app
from backend.app.retry import retry_after_seconds
from test_mvp import SETTINGS, TARGET, OfflineEncoding, final_content, github_response, run_simulated_agent, simulated_response, tool_call
from test_required_lookups import PolicyServer, prefix, step


def model_error(status, *, code=None, error_type=None, headers=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request, headers=headers)
    exception = {401: AuthenticationError, 429: RateLimitError}.get(status, APIStatusError)
    return exception("Private upstream response", response=response, body={"code": code, "type": error_type})


class RetryPolicyTests(unittest.TestCase):
    def test_retry_delay_supports_http_dates_and_github_reset_time(self):
        header = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
        self.assertLessEqual(retry_after_seconds({"retry-after": header}), 30)
        self.assertGreater(retry_after_seconds({"retry-after": header}), 25)
        reset = str(int(time.time()) + 40)
        throttled = httpx.Response(403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": reset, "Retry-After": "1",
        })
        self.assertGreater(github_rate_limit_delay(throttled), 35)
        forbidden = httpx.Response(403, json={"message": "Resource not accessible"}, headers={"Retry-After": "1"})
        self.assertIsNone(github_rate_limit_delay(forbidden))
        self.assertEqual(github_rate_limit_delay(httpx.Response(429)), 60)


class RetryAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_retry_allows_the_investigation_to_complete(self):
        (search,) = prefix()
        server = PolicyServer(empty=True)
        with patch.object(AgentRun, "_wait_for_retry", new=AsyncMock()) as wait:
            result, create, _ = await run_simulated_agent([
                model_error(503), search, step("search_issues", query="export"),
                simulated_response(content=final_content()),
            ], handler=server.respond, required_lookups=True)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.limitations, [])
        self.assertEqual(result.usage.model_requests, 4)
        self.assertEqual(create.call_args_list[0].kwargs["input"], create.call_args_list[1].kwargs["input"])
        wait.assert_awaited_once()

    async def test_model_retry_reuses_evidence_and_rechecks_the_last_request_slot(self):
        with patch.object(AgentRun, "_wait_for_retry", new=AsyncMock()) as wait:
            result, create, emit = await run_simulated_agent([
                model_error(429, code="rate_limit_exceeded", error_type="rate_limit_error"),
                simulated_response(content=final_content()),
            ], settings=replace(SETTINGS, max_iterations=2))
        self.assertIsNotNone(result.content)
        self.assertEqual(result.usage.model_requests, 2)
        self.assertEqual(result.usage.total_tokens, 25)
        self.assertEqual(create.call_args_list[0].kwargs["input"], create.call_args_list[1].kwargs["input"])
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertIn("request 2 of 2", create.call_args.kwargs["instructions"])
        self.assertTrue(any("OpenAI returned a response to the retry" in item.args[1] for item in emit.call_args_list))
        wait.assert_awaited_once()

    async def test_tool_retry_stops_when_the_wait_uses_up_the_job_time(self):
        readme_requests = 0

        def respond(request):
            nonlocal readme_requests
            if request.url.path.endswith("/readme"):
                readme_requests += 1
                return httpx.Response(503)
            return github_response(request)

        async def wait_past_the_deadline(run, retry):
            run.started -= run.settings.runtime_seconds + 1

        with patch.object(AgentRun, "_wait_for_retry", new=wait_past_the_deadline), self.assertRaises(TimeoutError):
            await run_simulated_agent([
                simulated_response([tool_call("get_repo_readme")]), simulated_response(content=final_content()),
            ], handler=respond)
        self.assertEqual(readme_requests, 1)

    async def interrupt_and_resume(self, responses, handler=github_response, settings=SETTINGS):
        saved = None

        async def persist(state):
            nonlocal saved
            saved = state.model_dump_json()

        sdk = AsyncMock()
        create = sdk.__aenter__.return_value.responses.create
        create.side_effect = responses
        start = AgentCheckpoint.start(checkpoint_signature(settings))
        github = httpx.MockTransport(handler)
        with (
            patch("backend.app.agent.AsyncOpenAI", return_value=sdk),
            patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
            patch.object(AgentRun, "_wait_for_retry", new=AsyncMock()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await AgentRun(TARGET, settings, lambda *_: None, github_transport=github,
                               checkpoint=start, save_checkpoint=persist).run()
            resumed = AgentCheckpoint.model_validate_json(saved)
            result = await AgentRun(TARGET, settings, lambda *_: None, github_transport=github,
                                    checkpoint=resumed, save_checkpoint=persist).run()
        return resumed, result, create

    async def test_interrupted_model_retry_cannot_gain_another_attempt_on_resume(self):
        resumed, result, create = await self.interrupt_and_resume([model_error(503), asyncio.CancelledError()])
        self.assertEqual(resumed.in_flight, "model")
        self.assertIsNotNone(resumed.retry)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(result.usage.model_requests, 2)
        self.assertEqual([source.id for source in result.sources], ["S1", "S2"])
        self.assertIsNone(result.content)
        self.assertTrue(any("retry was exhausted" in limitation for limitation in result.limitations))

    async def test_interrupted_candidate_retry_keeps_both_read_reservations_on_resume(self):
        server = PolicyServer()
        candidate_requests = 0

        def respond(request):
            nonlocal candidate_requests
            if request.url.path.endswith("/issues/41"):
                candidate_requests += 1
                if candidate_requests == 1:
                    return httpx.Response(503)
                if candidate_requests == 2:
                    raise asyncio.CancelledError
            return server.respond(request)

        resumed, result, _ = await self.interrupt_and_resume([
            *prefix(), step("get_related_issue", issue_number=41), step("get_related_issue", issue_number=41),
            simulated_response(content=final_content()),
        ], respond, replace(SETTINGS, max_candidate_reads=2))
        self.assertEqual(resumed.in_flight, "tool")
        self.assertIsNotNone(resumed.retry)
        self.assertEqual(resumed.tools.candidate_reads, 2)
        self.assertEqual(candidate_requests, 2)
        self.assertEqual(result.similar_issue_check.candidate_status, "failed")
        self.assertTrue(any("retry was exhausted" in limitation for limitation in result.limitations))
        self.assertTrue(any("full candidate read limit was reached" in limitation for limitation in result.limitations))


class RetryAPITests(unittest.TestCase):
    def analyze(self, responses, settings=SETTINGS, github_handler=github_response):
        github = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(github_handler))
        sdk = AsyncMock()
        create = sdk.__aenter__.return_value.responses.create
        create.side_effect = responses
        with (
            patch("backend.app.agent.httpx.AsyncClient", return_value=github),
            patch("backend.app.agent.AsyncOpenAI", return_value=sdk),
            patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
            patch.object(AgentRun, "_wait_for_retry", new=AsyncMock()) as wait,
            TestClient(create_app(settings)) as api,
        ):
            submitted = api.post("/api/v1/analyses", json={"issue_url": TARGET.url})
            self.assertEqual(submitted.status_code, 202)
            path = f"/api/v1/analyses/{submitted.json()['job_id']}"
            self.assertIn("event: done", api.get(f"{path}/events").text)
            response = api.get(f"{path}/trace")
            self.assertEqual(response.status_code, 200)
            trace = response.json()
            self.assertEqual(trace["job_id"], submitted.json()["job_id"])
            return api.get(path).json(), create.await_count, wait.await_count, trace

    def test_trace_links_successful_model_retry(self):
        snapshot, calls, waits, trace = self.analyze([
            model_error(503, headers={"Retry-After": "2"}), simulated_response(content=final_content()),
        ], replace(SETTINGS, max_iterations=2))
        self.assertEqual((snapshot["status"], calls, waits), ("partial", 2, 1))
        self.assertEqual(trace["stop_reason"], "final_answer")
        issue, labels, failed, retry = trace["attempts"]
        self.assertEqual([(item["kind"], item["name"], item["outcome"]) for item in (issue, labels, failed, retry)], [
            ("tool", "get_issue", "succeeded"), ("tool", "get_repo_labels", "succeeded"),
            ("model", "responses.create", "failed"), ("model", "responses.create", "succeeded"),
        ])
        self.assertEqual((failed["attempt_number"], retry["attempt_number"]), (1, 2))
        self.assertIsNotNone(failed["retry_scheduled_for"])
        self.assertIsNone(failed["stop_reason"])
        self.assertTrue(retry["is_retry"])
        self.assertEqual(retry["retry_of"], failed["id"])
        self.assertTrue(all(item["duration_ms"] >= 0 for item in trace["attempts"]))
        self.assertNotIn("Private upstream response", json.dumps(trace))

    def test_trace_reports_context_stop_without_an_unsent_attempt(self):
        snapshot, calls, waits, trace = self.analyze([], replace(SETTINGS, context_tokens=128))
        # The server's reads finished; the first model request did not fit and was never sent.
        self.assertEqual((snapshot["status"], calls, waits), ("partial", 0, 0))
        self.assertEqual(trace["stop_reason"], "context_budget")
        self.assertEqual([(item["kind"], item["name"]) for item in trace["attempts"]], [
            ("tool", "get_issue"), ("tool", "get_repo_labels"),
        ])

    def test_model_retry_and_job_budgets_preserve_partial_evidence_when_exhausted(self):
        for limit, failures, waits, stop_reason, reason in (
            (1, [model_error(503)], 0, "model_request_budget", "model request budget was exhausted"),
            (8, [model_error(503), model_error(503)], 1, "retry_exhausted", "the one retry was exhausted"),
            (8, [model_error(503, headers={"Retry-After": "600"})], 0,
             "retry_wait_exceeds_runtime", "required wait exceeds the remaining job time"),
        ):
            with self.subTest(reason=reason):
                snapshot, calls, retry_waits, trace = self.analyze(failures, replace(SETTINGS, max_iterations=limit))
                self.assertEqual(snapshot["status"], "partial")
                self.assertEqual(calls, len(failures))
                self.assertEqual(retry_waits, waits)
                self.assertEqual(snapshot["result"]["usage"]["model_requests"], calls)
                self.assertEqual([source["id"] for source in snapshot["result"]["sources"]], ["S1", "S2"])
                self.assertTrue(any(reason in item for item in snapshot["result"]["limitations"]))
                self.assertNotIn("Private upstream response", json.dumps(snapshot))
                self.assertEqual(trace["stop_reason"], stop_reason)
                last_attempt = trace["attempts"][-1]
                self.assertEqual((last_attempt["outcome"], last_attempt["stop_reason"]), ("failed", stop_reason))
                self.assertEqual(last_attempt["is_retry"], bool(waits))
                self.assertIsNone(last_attempt["retry_scheduled_for"])

    def test_permanent_model_errors_stop_after_one_request(self):
        for failure in (
            model_error(401),
            model_error(400, code="invalid_request_error"),
            model_error(429, code="insufficient_quota", error_type="insufficient_quota", headers={"Retry-After": "1"}),
            model_error(429, code="organization_usage_limit_exceeded"),
        ):
            with self.subTest(code=failure.code, status=failure.status_code):
                snapshot, calls, waits, trace = self.analyze([failure])
                # The server's reads are kept as partial evidence.
                self.assertEqual((snapshot["status"], calls, waits), ("partial", 1, 0))
                self.assertNotIn("Private upstream response", json.dumps(snapshot))
                attempt = trace["attempts"][-1]
                self.assertEqual([item["kind"] for item in trace["attempts"]], ["tool", "tool", "model"])
                self.assertEqual(trace["stop_reason"], "model_request_failed")
                self.assertEqual((attempt["outcome"], attempt["stop_reason"]), ("failed", "non_retryable_failure"))
                self.assertNotIn("Private upstream response", json.dumps(trace))

    def test_lost_openai_connection_keeps_the_server_reads_and_names_the_problem(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        snapshot, calls, waits, trace = self.analyze([APIConnectionError(request=request), APIConnectionError(request=request)])
        self.assertEqual((snapshot["status"], calls, waits), ("partial", 2, 1))
        self.assertEqual(snapshot["error"], "Could not complete the OpenAI request. Check connectivity and try again.")
        self.assertEqual(snapshot["result"]["limitations"][-1], snapshot["error"])
        self.assertEqual(trace["stop_reason"], "retry_exhausted")

    def test_unexpected_error_keeps_the_evidence_and_logs_only_its_type(self):
        with self.assertLogs("backend.app.jobs", level="ERROR") as logs:
            snapshot, calls, _, trace = self.analyze([RuntimeError("Private upstream detail")])
        self.assertEqual((snapshot["status"], calls), ("partial", 1))
        self.assertEqual(snapshot["error"], "The analysis could not be completed. Check server configuration and try again.")
        self.assertEqual(snapshot["result"]["limitations"][-1], snapshot["error"])
        self.assertEqual(trace["stop_reason"], "unexpected_error")
        self.assertEqual(logs.output, ["ERROR:backend.app.jobs:Analysis failed with RuntimeError"])
        self.assertNotIn("Private upstream detail", json.dumps(snapshot) + json.dumps(trace))

    def test_github_retry_uses_the_existing_tool_budget(self):
        label_requests = []

        def respond(request):
            if request.url.path.endswith("/labels"):
                label_requests.append(request)
                return httpx.Response(503)
            return github_response(request)

        snapshot, calls, waits, trace = self.analyze(
            [simulated_response(content=final_content())], replace(SETTINGS, max_tool_calls=2), respond,
        )
        self.assertEqual((snapshot["status"], calls, waits), ("partial", 1, 0))
        self.assertEqual(len(label_requests), 1)
        self.assertEqual(snapshot["result"]["usage"]["tool_calls"], 2)
        self.assertIsNotNone(snapshot["result"]["content"])
        self.assertTrue(any("tool-call budget was exhausted" in item for item in snapshot["result"]["limitations"]))
        self.assertEqual(trace["stop_reason"], "final_answer")
        tool_attempts = [item for item in trace["attempts"] if item["kind"] == "tool"]
        self.assertEqual(len(tool_attempts), 2)
        self.assertEqual((tool_attempts[-1]["outcome"], tool_attempts[-1]["stop_reason"]), ("failed", "tool_call_budget"))

    def test_github_cooldown_prevents_early_followup_after_a_long_retry_after(self):
        requests = []

        def respond(request):
            requests.append(request.url.path)
            if request.url.path.endswith("/labels"):
                return httpx.Response(429, headers={"Retry-After": "600"})
            return github_response(request)

        snapshot, _, waits, _ = self.analyze([
            simulated_response([tool_call("search_issues", '{"query":"export"}', "search")]),
            simulated_response(content=final_content()),
        ], SETTINGS, respond)
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(waits, 0)
        self.assertEqual(requests, [f"{TARGET.api_path}/issues/{TARGET.number}", f"{TARGET.api_path}/labels"])
        limitations = snapshot["result"]["limitations"]
        self.assertTrue(any("required wait exceeds the remaining job time" in item for item in limitations))
        self.assertTrue(any("cooldown is still active" in item for item in limitations))
