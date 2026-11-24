"""Offline label recommendation checks; no live GitHub or model calls are made."""

import asyncio
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

from backend.app.github import GitHubTools, ToolError
from backend.app.main import create_app
from backend.app.trace import TraceLog
from test_mvp import ISSUE, SETTINGS, TARGET, final_content, run_simulated_agent, simulated_response, tool_call


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "label_recommendations.json").read_text())
TARGET_ISSUE = {**ISSUE, "labels": [{"name": name} for name in FIXTURE["existing_labels"]]}


def label_github_response(request):
    if request.url.path.endswith("/labels"):
        return httpx.Response(200, json=FIXTURE["labels"])
    if request.url.path == "/search/issues":
        return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})
    return httpx.Response(200, json=TARGET_ISSUE)


def label_content():
    content = final_content()
    content["label_recommendations"] = copy.deepcopy(FIXTURE["recommendations"])
    return content


def label_prefix():
    """The model's search; the server reads the issue and the label catalog before it."""
    return [simulated_response([tool_call("search_issues", '{"query":"export"}', "call_search")])]


class RepositoryLabelToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.payload = copy.deepcopy(FIXTURE["labels"])
        self.label_headers = {}

        def handler(request):
            self.requests.append(request)
            if request.url.path.endswith("/labels"):
                return httpx.Response(200, json=self.payload, headers=self.label_headers)
            return httpx.Response(200, json=TARGET_ISSUE)

        self.client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        self.tools = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))
        await self.tools.execute("get_issue", "{}")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_catalog_is_read_only_repository_scoped_and_reuses_successful_reads(self):
        result = await self.tools.execute("get_repo_labels", "{}")
        await self.tools.execute("get_repo_labels", "{}")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[-1].url.path, "/repos/example/project/labels")
        self.assertEqual(self.requests[-1].url.params["per_page"], "100")
        self.assertTrue(all(request.method == "GET" for request in self.requests))
        self.assertEqual(result["source"]["url"], "https://github.com/example/project/labels")
        self.assertEqual(result["source"]["kind"], "repo_labels")
        self.assertEqual(result["existing_issue_labels"], ["question"])
        self.assertEqual(result["required_recommendation_source_ids"], ["S1", "S2"])
        self.assertEqual(list(self.tools.repo_labels), ["type:bug", "needs-reproduction", "question"])
        self.assertEqual(self.tools.repo_labels["question"].description, "")
        self.assertTrue(self.tools.labels_complete)
        self.assertEqual(len(json.loads(result["source"]["excerpt"])["labels"]), 3)

    async def test_scope_arguments_are_rejected_before_network(self):
        count = len(self.requests)
        with self.assertRaises(ValidationError):
            await self.tools.execute("get_repo_labels", '{"owner":"other"}')
        self.assertEqual(len(self.requests), count)

    async def test_target_must_be_read_before_catalog(self):
        fresh = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))
        count = len(self.requests)
        with self.assertRaisesRegex(ToolError, "Read the target"):
            await fresh.execute("get_repo_labels", "{}")
        self.assertEqual(len(self.requests), count)

    async def test_empty_catalog_is_a_successful_check(self):
        self.payload = []
        result = await self.tools.execute("get_repo_labels", "{}")
        self.assertEqual(self.tools.repo_labels, {})
        self.assertIsNotNone(self.tools.labels_source_id)
        self.assertTrue(self.tools.labels_complete)
        self.assertEqual(json.loads(result["source"]["excerpt"])["labels"], [])

    async def test_more_pages_are_not_silently_presented_as_complete(self):
        self.label_headers = {"Link": '<https://api.github.com/repos/example/project/labels?page=2>; rel="next"'}
        await self.tools.execute("get_repo_labels", "{}")
        self.assertFalse(self.tools.labels_complete)
        self.assertTrue(any("Only part" in item for item in self.tools.limitations))
        self.assertEqual(len(self.requests), 2)

    async def test_context_limit_keeps_complete_rows_and_restricts_the_allowlist(self):
        self.tools.clip = lambda text, limit: (text, "needs-reproduction" in text)
        result = await self.tools.execute("get_repo_labels", "{}")
        self.assertEqual(list(self.tools.repo_labels), ["type:bug"])
        self.assertFalse(self.tools.labels_complete)
        parsed = json.loads(result["source"]["excerpt"])
        self.assertEqual(len(parsed["labels"]), 1)
        self.assertEqual(parsed["labels"][0]["name"], "type:bug")

    async def test_rate_limit_is_a_failed_check_not_an_empty_catalog(self):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"message": "Rate limited"}),
        )) as client:
            self.tools.client = client
            with self.assertRaises(ToolError):
                await self.tools.execute("get_repo_labels", "{}")
        self.assertIsNone(self.tools.labels_source_id)
        self.assertIsNone(self.tools.labels_complete)


class LabelAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_recommendations_preserve_citations_and_catalog_metadata(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=label_content()),
        ], handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual([item.name for item in result.content.label_recommendations], ["type:bug", "needs-reproduction"])
        self.assertEqual(result.labels_source_id, "S2")
        self.assertEqual(len(result.repo_labels), 3)
        self.assertTrue(result.labels_complete)
        self.assertEqual(result.limitations, [])
        first = create.call_args_list[0].kwargs["input"]
        self.assertEqual(first[-1]["call_id"], "server_get_repo_labels")
        self.assertIn("Repository label catalog", first[-1]["output"])
        self.assertIn("model request 2 of 8", create.call_args.kwargs["instructions"])

    async def test_catalog_with_more_pages_is_traced_as_a_partial_lookup(self):
        def respond(request):
            if request.url.path.endswith("/labels"):
                more = {"Link": f'<https://api.github.com{TARGET.api_path}/labels?page=2>; rel="next"'}
                return httpx.Response(200, json=FIXTURE["labels"], headers=more)
            return label_github_response(request)

        trace = TraceLog()
        result, _, _ = await run_simulated_agent(
            [*label_prefix(), simulated_response(content=final_content())], handler=respond, trace=trace,
        )
        (attempt,) = [item for item in trace.attempts if item.name == "get_repo_labels"]
        self.assertEqual((attempt.outcome, attempt.reason), ("partial", "The lookup returned partial data."))
        self.assertFalse(result.labels_complete)

    async def test_exhausted_validation_explains_why_suggestions_were_not_accepted(self):
        content = label_content()
        content["label_recommendations"][0]["source_ids"] = ["S2"]
        result, _, emit = await run_simulated_agent([
            *label_prefix(), simulated_response(content=content),
        ], settings=replace(SETTINGS, max_iterations=2), handler=label_github_response)
        self.assertIsNone(result.content)
        warnings = [call.args[1] for call in emit.call_args_list if call.args[0] == "warning"]
        self.assertTrue(any("cite the target issue and label catalog" in warning for warning in warnings))
        self.assertTrue(any("No correction budget remains" in warning for warning in warnings))

    async def test_names_are_returned_in_the_catalogs_canonical_case(self):
        content = label_content()
        content["label_recommendations"][0]["name"] = "TYPE:BUG"
        result, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=content),
        ], handler=label_github_response)
        self.assertEqual(result.content.label_recommendations[0].name, "type:bug")

    async def test_invalid_recommendations_require_a_correction(self):
        for problem in ("unknown", "archived", "already_applied", "duplicate", "blank_reason", "missing_target", "missing_catalog", "fabricated_source", "too_many"):
            with self.subTest(problem=problem):
                content = label_content()
                first = content["label_recommendations"][0]
                if problem == "unknown":
                    first["name"] = "invented-label"
                elif problem == "archived":
                    first["name"] = "legacy-bug"
                elif problem == "already_applied":
                    first["name"] = "QUESTION"
                elif problem == "duplicate":
                    content["label_recommendations"].append({**first, "name": "TYPE:BUG"})
                elif problem == "blank_reason":
                    first["reason"] = " "
                elif problem == "missing_target":
                    first["source_ids"] = ["S2"]
                elif problem == "missing_catalog":
                    first["source_ids"] = ["S1", "S3"]
                elif problem == "fabricated_source":
                    first["source_ids"].append("S999")
                else:
                    content["label_recommendations"] = [copy.deepcopy(first) for _ in range(4)]
                result, create, emit = await run_simulated_agent([
                    *label_prefix(), simulated_response(content=content), simulated_response(content=label_content()),
                ], handler=label_github_response)
                self.assertIsNotNone(result.content)
                self.assertEqual(create.await_count, 3)
                self.assertEqual(len(result.content.label_recommendations), 2)
                self.assertTrue(any(call.args[0] == "warning" for call in emit.call_args_list))

    async def test_no_additional_suitable_label_can_return_no_recommendations(self):
        result, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=final_content()),
        ], handler=label_github_response)
        self.assertEqual(result.content.label_recommendations, [])
        self.assertIsNotNone(result.labels_source_id)
        self.assertEqual(result.limitations, [])

    async def test_empty_catalog_can_finish_with_no_recommendations(self):
        def no_labels(request):
            if request.url.path.endswith("/labels"):
                return httpx.Response(200, json=[])
            return label_github_response(request)

        result, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=final_content()),
        ], handler=no_labels)
        self.assertEqual(result.content.label_recommendations, [])
        self.assertEqual(result.repo_labels, [])
        self.assertTrue(result.labels_complete)
        self.assertEqual(result.limitations, [])

    async def test_unretrieved_catalog_cannot_support_recommendations(self):
        def labels_unavailable(request):
            if request.url.path.endswith("/labels"):
                return httpx.Response(404, json={"message": "Not Found"})
            return label_github_response(request)

        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=label_content()), simulated_response(content=final_content()),
        ], handler=labels_unavailable)
        self.assertEqual(result.content.label_recommendations, [])
        self.assertIsNone(result.labels_source_id)
        self.assertTrue(any("label catalog was not retrieved" in item for item in result.limitations))
        self.assertIn("get_repo_labels", create.call_args.kwargs["instructions"])


class LabelAPITests(unittest.TestCase):
    def test_label_suggestions_and_catalog_survive_job_and_sse_serialization(self):
        result, _, _ = asyncio.run(run_simulated_agent([
            *label_prefix(), simulated_response(content=label_content()),
        ], handler=label_github_response))
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=result)):
            with TestClient(create_app(SETTINGS)) as client:
                created = client.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                self.assertEqual(created.status_code, 202)
                job_id = created.json()["job_id"]
                stream = client.get(f"/api/v1/analyses/{job_id}/events")
                self.assertIn("event: done", stream.text)
                snapshot = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(snapshot["status"], "completed")
                self.assertEqual(snapshot["result"]["labels_source_id"], "S2")
                self.assertTrue(snapshot["result"]["labels_complete"])
                self.assertEqual(len(snapshot["result"]["repo_labels"]), 3)
                self.assertEqual(snapshot["result"]["content"]["label_recommendations"][0]["name"], "type:bug")


if __name__ == "__main__":
    unittest.main()
