"""Offline integration checks. All model responses and GitHub requests are simulated."""

import asyncio
import base64
from contextlib import nullcontext
from dataclasses import replace
import json
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
import httpx
from openai.types.responses import ResponseFunctionToolCall, ResponseReasoningItem
from pydantic import ValidationError

from backend.app.agent import AgentRun
from backend.app.config import GitHubOAuthConfig, ROOT, Settings
from backend.app.github import GitHubTools, ToolError
from backend.app.main import create_app
from backend.app.models import AnalysisContent, AnalysisResult, IssueTarget, Source


TARGET = IssueTarget.from_url("https://github.com/example/project/issues/42")
SETTINGS = Settings(openai_api_key="offline-test-key", context_tokens=32_000)
ISSUE = {
    "title": "Export fails on Windows", "body": "Version 1.4 on Windows 11. Export closes the app.",
    "state": "open", "number": 42, "user": {"login": "reporter"},
    "labels": [{"name": "bug"}], "comments": 0,
}
COMMENT = {
    "id": 101, "user": {"login": "maintainer"}, "body": "Please share a minimal reproduction.",
    "author_association": "MEMBER", "created_at": "2026-11-01T00:00:00Z",
}


def final_content(source_id="S1"):
    return {
        "summary": {"text": "The reporter describes an export failure on Windows.", "source_ids": [source_id]},
        "known_facts": [{"text": "Version 1.4 and Windows 11 were reported.", "source_ids": [source_id]}],
        "missing_information": ["Can you share a minimal reproduction?"],
        "draft_reply": "Thanks for reporting this. Could you share a minimal reproduction?",
        "limitations": ["The failure has not been reproduced."],
        "comparisons": [],
        "label_recommendations": [],
    }


def simulated_response(items=(), content=None, status="completed", incomplete_reason=None):
    return SimpleNamespace(
        output=list(items), status=status,
        output_text=json.dumps(content) if content else "",
        usage=SimpleNamespace(total_tokens=25),
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
    )


def tool_call(name, arguments="{}", call_id="call_offline_1"):
    return ResponseFunctionToolCall(type="function_call", name=name, arguments=arguments, call_id=call_id)


class OfflineEncoding:
    """A deterministic test stand-in, not a model tokenizer or billing estimate."""

    def encode(self, text, disallowed_special=()):
        return list(text.encode("utf-8"))

    def decode(self, tokens):
        return bytes(tokens).decode("utf-8", errors="replace")


def github_response(request):
    if request.url.path.endswith("/comments"):
        return httpx.Response(200, json=[])
    if request.url.path.endswith("/labels"):
        return httpx.Response(200, json=[])
    if request.url.path.endswith("/readme"):
        return httpx.Response(200, json={
            "content": base64.b64encode(b"Project README").decode(), "encoding": "base64",
            "size": 14, "path": "README.md", "html_url": "https://github.com/example/project/blob/main/README.md",
        })
    return httpx.Response(200, json=ISSUE)


def commented_github_response(request):
    """Use positive metadata and a matching comment page for comment-specific checks."""
    if request.url.path.endswith("/comments"):
        return httpx.Response(200, json=[COMMENT])
    if request.url.path.endswith("/issues/42"):
        return httpx.Response(200, json={**ISSUE, "comments": 1})
    return github_response(request)


class TargetTests(unittest.TestCase):
    def test_copied_issue_links_are_canonicalized(self):
        target = IssueTarget.from_url(" https://github.com/example/project/issues/42/?foo=bar#issuecomment-1 ")
        self.assertEqual(target.url, TARGET.url)

    def test_non_issue_and_untrusted_urls_are_rejected(self):
        urls = [
            "http://github.com/example/project/issues/42",
            "https://github.com.evil.test/example/project/issues/42",
            "https://github.com@127.0.0.1/example/project/issues/42",
            "https://github.com:443/example/project/issues/42",
            "https://github.com/example/project/pull/42",
            "https://github.com/example/../issues/42",
            "https://github.com/example/project%2f..%2fsecret/issues/42",
            "https://github.com/example/project/issues/0",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                IssueTarget.from_url(url)


class SettingsTests(unittest.TestCase):
    def test_credentials_never_appear_in_settings_repr(self):
        settings = Settings(
            openai_api_key="SENSITIVE_OPENAI_CANARY", github_token="SENSITIVE_GITHUB_CANARY",
            database_url="postgresql://user:SENSITIVE_DATABASE_CANARY@localhost/project",
            github_oauth=GitHubOAuthConfig(
                "offline-client", "SENSITIVE_OAUTH_CANARY", "http://127.0.0.1:3000/api/v1/auth/github/callback",
            ),
        )
        text = repr(settings)
        for canary in ("OPENAI", "GITHUB", "DATABASE", "OAUTH"):
            self.assertNotIn(f"SENSITIVE_{canary}_CANARY", text)
        # Non-secret settings stay visible for debugging.
        self.assertIn(f"model={settings.model!r}", text)

    def test_importing_the_api_module_reads_no_configuration(self):
        # Importing must not load .env, or every test process would hold the real credentials.
        # A subprocess is needed because this test process imported the module long ago.
        probe = (
            "import backend.app.config as config\n"
            "calls = []\n"
            "config.load_dotenv = lambda *args, **kwargs: calls.append(args)\n"
            "import backend.app.main\n"
            "print(len(calls))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True, timeout=60,
        )
        self.assertEqual(result.stdout.strip(), "0")


class GitHubToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        def handler(request):
            self.requests.append(request)
            return commented_github_response(request)

        self.client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        self.tools = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_tools_collect_canonical_sources_and_reuse_reads(self):
        first = await self.tools.execute("get_issue", "{}")
        await self.tools.execute("get_issue", "{}")
        comments = await self.tools.execute("get_issue_comments", '{"page":1}')
        readme = await self.tools.execute("get_repo_readme", "{}")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(first["source"]["id"], "S1")
        self.assertEqual(comments["comments"][0]["url"], f"{TARGET.url}#issuecomment-101")
        self.assertEqual(readme["source"]["id"], "S3")
        self.assertTrue(all(request.method == "GET" and request.url.host == "api.github.com" for request in self.requests))

    async def test_unknown_tools_and_invalid_arguments_never_make_requests(self):
        for name, args in [
            ("run_shell", "{}"), ("get_issue", '{"owner":"other"}'),
            ("get_issue_comments", '{"page":6}'), ("get_issue_comments", '{"page":"1"}'),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises((ToolError, ValidationError)):
                await self.tools.execute(name, args)
        self.assertEqual(self.requests, [])

    async def test_rate_limit_is_an_explicit_tool_error(self):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"message": "Rate limited"}),
        )) as client:
            tools = GitHubTools(client, TARGET, lambda text, limit: (text, False))
            with self.assertRaisesRegex(ToolError, "rate limit"):
                await tools.execute("get_issue", "{}")
            self.assertEqual(tools.sources, {})

    async def test_pull_requests_are_not_accepted_as_issues(self):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={**ISSUE, "pull_request": {}}),
        )) as client:
            tools = GitHubTools(client, TARGET, lambda text, limit: (text, False))
            with self.assertRaisesRegex(ToolError, "pull request"):
                await tools.execute("get_issue", "{}")

    async def test_truncation_is_recorded_as_a_limitation(self):
        self.tools.clip = lambda text, limit: (text[:5], True)
        result = await self.tools.execute("get_issue", "{}")
        self.assertEqual(len(result["source"]["excerpt"]), 5)
        self.assertIn("truncated", self.tools.limitations[0])

    async def test_unexpected_readme_links_are_rejected(self):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "encoding": "base64", "size": 1, "html_url": "https://evil.test/fake", "content": "YQ==",
            }),
        )) as client:
            tools = GitHubTools(client, TARGET, lambda text, limit: (text, False))
            with self.assertRaisesRegex(ToolError, "unexpected"):
                await tools.execute("get_repo_readme", "{}")


async def run_simulated_agent(
    responses, settings=SETTINGS, handler=github_response, encoding=None, *, required_lookups=False, trace=None,
):
    """Isolate existing feature validators; new gate integration tests opt in explicitly."""
    github = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    create = AsyncMock(side_effect=responses)
    client = AsyncMock()
    client.__aenter__.return_value.responses.create = create
    emit = Mock()
    lookup_policy = nullcontext() if required_lookups else patch.object(AgentRun, "required_lookup", return_value=None)
    with (
        patch("backend.app.agent.httpx.AsyncClient", return_value=github),
        patch("backend.app.agent.AsyncOpenAI", return_value=client),
        patch("backend.app.agent.tiktoken.get_encoding", return_value=encoding if encoding is not None else OfflineEncoding()),
        lookup_policy,
    ):
        result = await AgentRun(TARGET, settings, emit, trace=trace).run()
    return result, create, emit


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def run_agent(self, responses, settings=SETTINGS):
        return await run_simulated_agent(responses, settings)

    async def test_skipping_similar_issue_search_preserves_analysis_with_a_limitation(self):
        result, create, _ = await self.run_agent([simulated_response(content=final_content())])
        self.assertIsNotNone(result.content)
        # The server read the issue and the label catalog; the model made one request.
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual(create.await_count, 1)
        self.assertEqual(create.call_args_list[0].kwargs["tool_choice"], "auto")
        self.assertEqual([source.kind for source in result.sources], ["issue", "repo_labels"])
        self.assertIn("A similar-issue search was not completed.", result.limitations)

    async def test_server_lookups_precede_the_first_request_and_model_decisions_keep_their_state(self):
        reasoning = ResponseReasoningItem(id="rs_offline", type="reasoning", summary=[], encrypted_content="opaque-offline-state")
        readme_call = tool_call("get_repo_readme", "{}", "call_offline_2")
        result, create, emit = await run_simulated_agent([
            simulated_response([reasoning, readme_call]), simulated_response(content=final_content()),
        ], handler=commented_github_response)
        first = create.call_args_list[0].kwargs["input"]
        self.assertEqual([(item["type"], item["call_id"]) for item in first[1:]], [
            ("function_call", "server_get_issue"), ("function_call_output", "server_get_issue"),
            ("function_call", "server_get_issue_comments"), ("function_call_output", "server_get_issue_comments"),
            ("function_call", "server_get_repo_labels"), ("function_call_output", "server_get_repo_labels"),
        ])
        self.assertEqual(json.loads(first[3]["arguments"]), {"page": 1})
        self.assertIn(("status", (
            "Reading the target issue, its first and latest comments pages if it has comments, "
            "and the label catalog before the first model request."
        )), [call.args for call in emit.call_args_list])
        replay = create.call_args_list[1].kwargs["input"]
        self.assertEqual(replay[7], reasoning.to_dict())
        self.assertEqual(replay[8], readme_call.to_dict())
        self.assertEqual(replay[9]["call_id"], readme_call.call_id)
        self.assertEqual(result.usage.model_requests, 2)
        self.assertEqual(result.usage.total_tokens, 50)
        self.assertEqual(result.usage.tool_calls, 4)
        self.assertEqual([source.kind for source in result.sources], ["issue", "comment", "repo_labels", "readme"])

    async def test_fabricated_source_ids_require_a_correction(self):
        result, create, emit = await self.run_agent([
            simulated_response(content=final_content("S999")), simulated_response(content=final_content()),
        ])
        self.assertEqual(create.await_count, 2)
        self.assertIsNotNone(result.content)
        self.assertTrue(any(call.args[0] == "warning" for call in emit.call_args_list))
        self.assertIn("Available IDs", create.call_args.kwargs["instructions"])

    async def test_no_model_request_without_successfully_reading_the_issue(self):
        result, create, _ = await run_simulated_agent(
            [simulated_response(content=final_content())],
            handler=lambda request: httpx.Response(404, json={"message": "Not Found"}),
        )
        create.assert_not_awaited()
        self.assertIsNone(result.content)
        self.assertEqual(result.sources, [])
        # Nothing else is read for an issue that cannot be read.
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(result.limitations[-1], "The target issue could not be read, so no model request was sent.")

    async def test_tool_budget_disables_more_tools_and_marks_partial_work(self):
        result, create, _ = await self.run_agent(
            [simulated_response(content=final_content())], replace(SETTINGS, max_tool_calls=1),
        )
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertIn(
            "The repository label catalog was not retrieved; label recommendations were not checked.", result.limitations,
        )

    async def test_context_budget_stops_before_another_model_request(self):
        result, create, _ = await self.run_agent([], replace(SETTINGS, context_tokens=1))
        create.assert_not_awaited()
        self.assertIn("context budget", result.limitations[-1])

    async def test_incomplete_responses_do_not_execute_tools(self):
        result, _, _ = await self.run_agent([simulated_response([tool_call("get_repo_readme")], status="incomplete")])
        self.assertEqual([source.kind for source in result.sources], ["issue", "repo_labels"])
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertIn("incomplete", result.limitations[-1])


def offline_result():
    return AnalysisResult(
        content=AnalysisContent.model_validate(final_content()),
        sources=[Source(id="S1", kind="issue", title="Offline fixture", url=TARGET.url, excerpt=ISSUE["body"])],
    )


class APITests(unittest.TestCase):
    def test_invalid_urls_and_missing_credentials_do_not_create_jobs(self):
        with TestClient(create_app(Settings())) as client:
            self.assertEqual(client.post("/api/v1/analyses", json={"issue_url": "http://127.0.0.1/private"}).status_code, 422)
            self.assertEqual(client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).status_code, 503)

    def test_async_job_result_and_resumable_event_stream(self):
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=offline_result())):
            with TestClient(create_app(SETTINGS)) as client:
                response = client.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                self.assertEqual(response.status_code, 202)
                job_id = response.json()["job_id"]
                events = client.get(f"/api/v1/analyses/{job_id}/events")
                self.assertIn("event: done", events.text)
                self.assertIn('"status": "completed"', events.text)
                result = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["result"]["sources"][0]["id"], "S1")
                resumed = client.get(f"/api/v1/analyses/{job_id}/events", headers={"Last-Event-ID": "2"})
                self.assertNotIn("id: 1\n", resumed.text)
                self.assertIn("event: done", resumed.text)

    def test_running_jobs_can_be_cancelled_and_queue_capacity_is_enforced(self):
        async def wait_for_cancellation(*args):
            await asyncio.sleep(60)

        with patch("backend.app.jobs.AgentRun.run", new=wait_for_cancellation):
            with TestClient(create_app(replace(SETTINGS, max_jobs=1))) as client:
                first = client.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                self.assertEqual(client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).status_code, 429)
                cancelled = client.delete(f"/api/v1/analyses/{first.json()['job_id']}")
                self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_timeout_preserves_partial_evidence(self):
        async def slow_run(run):
            run.result = offline_result()
            run.trace.begin("model", "responses.create", 1, None)
            await asyncio.sleep(60)

        with patch("backend.app.jobs.AgentRun.run", new=slow_run):
            with TestClient(create_app(replace(SETTINGS, runtime_seconds=0.01))) as client:
                response = client.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                job_id = response.json()["job_id"]
                events = client.get(f"/api/v1/analyses/{job_id}/events")
                self.assertIn("event: done", events.text)
                result = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(result["status"], "partial")
                self.assertEqual(len(result["result"]["sources"]), 1)
                self.assertIn("runtime limit", result["result"]["limitations"][0])
                trace = client.get(f"/api/v1/analyses/{job_id}/trace").json()
                (attempt,) = trace["attempts"]
                self.assertEqual(trace["stop_reason"], "job_runtime_limit")
                self.assertEqual((attempt["outcome"], attempt["stop_reason"]), ("timed_out", "job_runtime_limit"))
                self.assertGreaterEqual(attempt["duration_ms"], 0)

    def test_unknown_jobs_return_404(self):
        with TestClient(create_app(SETTINGS)) as client:
            self.assertEqual(client.get("/api/v1/analyses/unknown").status_code, 404)
            self.assertEqual(client.get("/api/v1/analyses/unknown/events").status_code, 404)
            self.assertEqual(client.get("/api/v1/analyses/unknown/trace").status_code, 404)


if __name__ == "__main__":
    unittest.main()
