"""Offline M2 checks: deterministic fixtures, simulated GitHub and model responses."""

import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx

from backend.app.github import GitHubTools, ToolError
from backend.app.main import create_app
from test_mvp import SETTINGS, TARGET, final_content, run_simulated_agent, simulated_response, tool_call


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "similar_issues.json").read_text())


def m2_github_response(request):
    if request.url.path == "/search/issues":
        return httpx.Response(200, json={
            "total_count": 4, "incomplete_results": False,
            "items": [FIXTURE["target"], *FIXTURE["candidates"]],
        })
    number = int(request.url.path.rsplit("/", 1)[-1])
    issue = next(item for item in [FIXTURE["target"], *FIXTURE["candidates"]] if item["number"] == number)
    return httpx.Response(200, json=issue)


def m2_content():
    result = final_content()
    result["comparisons"] = copy.deepcopy(FIXTURE["comparisons"])
    return result


def investigation_prefix(numbers=(41, 43, 44)):
    return [
        simulated_response([tool_call("get_issue")]),
        simulated_response([tool_call("search_issues", '{"query":"export"}', "call_search")]),
        *[simulated_response([tool_call("get_related_issue", json.dumps({"issue_number": number}), f"call_candidate_{number}")]) for number in numbers],
    ]


class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.search_payload = {"total_count": 4, "incomplete_results": False, "items": [FIXTURE["target"], *FIXTURE["candidates"]]}

        def handler(request):
            self.requests.append(request)
            if request.url.path == "/search/issues":
                return httpx.Response(200, json=self.search_payload)
            return m2_github_response(request)

        self.client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        self.tools = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))
        await self.tools.execute("get_issue", "{}")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_search_is_repository_scoped_and_filters_self_prs_and_other_repositories(self):
        self.search_payload["items"] += [
            {**FIXTURE["candidates"][0], "number": 99, "pull_request": {}},
            {**FIXTURE["candidates"][0], "number": 100, "repository_url": "https://api.github.com/repos/other/project"},
        ]
        result = await self.tools.execute("search_issues", '{"query":"export"}')
        query = self.requests[-1].url.params["q"]
        self.assertEqual(query, 'repo:example/project is:issue in:title,body "export"')
        self.assertEqual(result["search"]["candidate_numbers"], [41, 43, 44])
        self.assertEqual(len(self.tools.sources), 2)
        self.assertTrue(all(candidate.source_id is None for candidate in self.tools.candidates.values()))

    async def test_model_supplied_qualifiers_cannot_change_repository_scope(self):
        await self.tools.execute("search_issues", '{"query":"repo:other/project OR export"}')
        query = self.requests[-1].url.params["q"]
        self.assertEqual(query.count("repo:"), 1)
        self.assertIn('"repo" "other" "project" "OR" "export"', query)
        self.assertEqual(list(self.tools.candidates), [41, 43, 44])

    async def test_repeated_searches_and_candidate_reads_reuse_results(self):
        await self.tools.execute("search_issues", '{"query":"export"}')
        await self.tools.execute("search_issues", '{"query":"export"}')
        first = await self.tools.execute("get_related_issue", '{"issue_number":41}')
        await self.tools.execute("get_related_issue", '{"issue_number":41}')
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.tools.searches), 1)
        self.assertEqual(self.tools.candidate_reads, 1)
        self.assertEqual(first["source"]["kind"], "related_issue")

    async def test_undiscovered_numbers_are_rejected_without_a_request(self):
        count = len(self.requests)
        for number in (41, 42, 999):
            with self.subTest(number=number), self.assertRaises(ToolError):
                await self.tools.execute("get_related_issue", json.dumps({"issue_number": number}))
        self.assertEqual(len(self.requests), count)

    async def test_full_candidate_read_budget_is_enforced_before_network(self):
        self.tools.max_candidate_reads = 1
        await self.tools.execute("search_issues", '{"query":"export"}')
        await self.tools.execute("get_related_issue", '{"issue_number":41}')
        count = len(self.requests)
        with self.assertRaisesRegex(ToolError, "read limit"):
            await self.tools.execute("get_related_issue", '{"issue_number":43}')
        self.assertEqual(len(self.requests), count)

    async def test_candidate_capacity_and_incomplete_search_are_explicit(self):
        self.tools.max_candidates = 1
        self.search_payload["incomplete_results"] = True
        self.search_payload["total_count"] = 100
        result = await self.tools.execute("search_issues", '{"query":"export"}')
        self.assertEqual(len(self.tools.candidates), 1)
        self.assertTrue(result["search"]["incomplete_results"])
        self.assertTrue(any("candidate limit" in item for item in self.tools.limitations))
        self.assertTrue(any("first page" in item for item in self.tools.limitations))
        self.assertTrue(any("incomplete search" in item for item in self.tools.limitations))

    async def test_search_failure_is_not_a_successful_empty_search(self):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"message": "Search quota reached"}),
        )) as client:
            self.tools.client = client
            with self.assertRaises(ToolError):
                await self.tools.execute("search_issues", '{"query":"export"}')
        self.assertEqual(self.tools.searches, [])

    async def test_candidate_identity_must_match_the_discovered_issue(self):
        await self.tools.execute("search_issues", '{"query":"export"}')
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={**FIXTURE["candidates"][0], "number": 999}),
        )) as client:
            self.tools.client = client
            with self.assertRaisesRegex(ToolError, "identity"):
                await self.tools.execute("get_related_issue", '{"issue_number":41}')
        self.assertIsNone(self.tools.candidates[41].source_id)


class ComparisonAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_relationships_retain_both_issue_sources_and_call_ids(self):
        result, create, _ = await run_simulated_agent(
            [*investigation_prefix(), simulated_response(content=m2_content())], handler=m2_github_response,
        )
        self.assertIsNotNone(result.content)
        self.assertEqual([item.relationship for item in result.content.comparisons], [
            "possible_duplicate", "related_but_different", "insufficient_evidence",
        ])
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual(len(result.sources), 5)
        self.assertEqual(result.limitations, ["The repository label catalog was not retrieved; label recommendations were not checked."])
        self.assertEqual(result.usage.model_requests, 6)
        self.assertEqual(result.usage.tool_calls, 5)
        self.assertEqual(create.call_args.kwargs["input"][-1]["call_id"], "call_candidate_44")

    async def test_invalid_comparison_contracts_require_correction(self):
        for problem in ("duplicate_number", "blank_reason", "blank_similarity", "missing_difference"):
            with self.subTest(problem=problem):
                invalid = m2_content()
                if problem == "duplicate_number":
                    invalid["comparisons"].append(copy.deepcopy(invalid["comparisons"][0]))
                elif problem == "blank_reason":
                    invalid["comparisons"][0]["reason"] = " "
                elif problem == "blank_similarity":
                    invalid["comparisons"][0]["similarities"] = [" "]
                else:
                    invalid["comparisons"][1]["differences"] = []
                result, create, _ = await run_simulated_agent([
                    *investigation_prefix(), simulated_response(content=invalid), simulated_response(content=m2_content()),
                ], handler=m2_github_response)
                self.assertIsNotNone(result.content)
                self.assertEqual(create.await_count, 7)
                self.assertIn("Final-answer repair feedback", create.call_args.kwargs["instructions"])


    async def test_search_hit_alone_cannot_support_a_comparison(self):
        invalid = m2_content()
        valid = final_content()
        result, create, _ = await run_simulated_agent([
            *investigation_prefix(numbers=()), simulated_response(content=invalid), simulated_response(content=valid),
        ], handler=m2_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.content.comparisons, [])
        self.assertIn("get_related_issue", create.call_args.kwargs["instructions"])
        self.assertTrue(any("no grounded comparison" in item for item in result.limitations))

    async def test_comparison_must_cite_target_and_full_candidate_not_search_source(self):
        invalid = m2_content()
        invalid["comparisons"][0]["source_ids"] = ["S1", "S2"]
        result, create, _ = await run_simulated_agent([
            *investigation_prefix(), simulated_response(content=invalid), simulated_response(content=m2_content()),
        ], handler=m2_github_response)
        self.assertIsNotNone(result.content)
        self.assertIn("both the target and full candidate", create.call_args.kwargs["instructions"])

    async def test_successful_empty_search_is_recorded_without_claiming_no_duplicate_exists(self):
        def empty_search(request):
            if request.url.path == "/search/issues":
                return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})
            return m2_github_response(request)

        result, _, _ = await run_simulated_agent([
            *investigation_prefix(numbers=()), simulated_response(content=final_content()),
        ], handler=empty_search)
        self.assertEqual(result.candidates, [])
        self.assertEqual(len(result.searches), 1)
        self.assertEqual(result.searches[0].total_count, 0)
        self.assertEqual(result.limitations, ["The repository label catalog was not retrieved; label recommendations were not checked."])


class ComparisonAPITests(unittest.TestCase):
    def test_candidates_searches_and_comparisons_survive_job_and_sse_serialization(self):
        result, _, _ = asyncio.run(run_simulated_agent([
            *investigation_prefix(), simulated_response(content=m2_content()),
        ], handler=m2_github_response))
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=result)):
            with TestClient(create_app(SETTINGS)) as client:
                created = client.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                self.assertEqual(created.status_code, 202)
                job_id = created.json()["job_id"]
                stream = client.get(f"/api/v1/analyses/{job_id}/events")
                self.assertIn("event: done", stream.text)
                snapshot = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(snapshot["status"], "partial")
                self.assertEqual(len(snapshot["result"]["candidates"]), 3)
                self.assertEqual(snapshot["result"]["searches"][0]["candidate_numbers"], [41, 43, 44])
                self.assertEqual(snapshot["result"]["content"]["comparisons"][2]["relationship"], "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
