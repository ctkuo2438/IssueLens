"""Full minimum-lookup policy tests with simulated model and GitHub APIs."""

import asyncio
from dataclasses import replace
from itertools import count
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx

from backend.app.main import create_app
from backend.app.agent import AgentRun, CANDIDATE_RESERVE_TOKENS, CONTINUATION_RESERVE_TOKENS, PENDING_LOOKUP_RESERVE_TOKENS
from scripts.acceptance import functional_checks
from test_labels import FIXTURE, TARGET_ISSUE
from test_mvp import COMMENT, SETTINGS, TARGET, final_content, run_simulated_agent, simulated_response, tool_call


CALL_IDS = count()
CANDIDATE = {
    "number": 41, "title": "Export fails on Linux", "state": "closed",
    "repository_url": "https://api.github.com/repos/example/project",
    "body": "Version 1.3 on Linux. Export closes the application.",
}


def step(name, **arguments):
    return simulated_response([tool_call(name, json.dumps(arguments), f"policy_{next(CALL_IDS)}")])


def content_with_comparison(source_id="S4"):
    content = final_content()
    content["comparisons"] = [{
        "issue_number": 41, "relationship": "related_but_different",
        "reason": "The target explicitly reports Windows while the candidate explicitly reports Linux.",
        "similarities": ["Both report an application closing during export."],
        "differences": ["The retrieved reports name different platforms and versions."],
        "source_ids": ["S1", source_id],
    }]
    return content


class PolicyServer:
    def __init__(self, *, empty=False, comments=0):
        self.requests = []
        self.empty = empty
        self.comments = comments
        self.search_status = 200
        self.labels_status = 200
        self.candidate_statuses = [200]
        self.candidate_calls = 0
        self.malformed_search = None
        self.malformed_candidate = False

    def respond(self, request):
        self.requests.append(request)
        if request.url.path == "/search/issues":
            data = self.malformed_search if self.malformed_search is not None else {
                "total_count": 0 if self.empty else 1, "incomplete_results": False,
                "items": [] if self.empty else [CANDIDATE],
            }
            return httpx.Response(self.search_status, json=data)
        if request.url.path.endswith("/labels"):
            return httpx.Response(self.labels_status, json=FIXTURE["labels"])
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[COMMENT] if self.comments else [])
        if request.url.path.endswith("/issues/41"):
            index = min(self.candidate_calls, len(self.candidate_statuses) - 1)
            self.candidate_calls += 1
            data = {**CANDIDATE, "body": []} if self.malformed_candidate else CANDIDATE
            return httpx.Response(self.candidate_statuses[index], json=data)
        return httpx.Response(200, json={**TARGET_ISSUE, "comments": self.comments})


async def run_policy(responses, server, settings=SETTINGS):
    return await run_simulated_agent(responses, settings=settings, handler=server.respond, required_lookups=True)


def prefix():
    return [step("get_issue"), step("get_repo_labels"), step("search_issues", query="export crash")]


class RequiredLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_catalog_search_and_candidate_precede_a_grounded_final(self):
        result, create, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        self.assertIsNotNone(result.content)
        self.assertEqual([call.kwargs["tool_choice"] for call in create.call_args_list[1:4]], [
            {"type": "function", "name": "get_repo_labels"},
            {"type": "function", "name": "search_issues"},
            {"type": "function", "name": "get_related_issue"},
        ])
        state = result.similar_issue_check
        self.assertEqual(state.search_status, "candidates_found")
        self.assertEqual(state.candidate_status, "read")
        self.assertEqual(state.read_candidate_numbers, [41])
        self.assertEqual(state.compared_candidate_numbers, [41])
        self.assertEqual(state.comparison_status, "compared")
        self.assertTrue(functional_checks(result)["minimum_similar_issue_checks_completed"])
        self.assertEqual(result.limitations, [])

    async def test_positive_comment_count_keeps_first_page_ahead_of_other_lookups(self):
        result, create, _ = await run_policy([
            step("get_issue"), step("get_issue_comments", page=1), step("get_repo_labels"),
            step("search_issues", query="export"), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison("S5")),
        ], PolicyServer(comments=1))
        self.assertIsNotNone(result.content)
        self.assertEqual(create.call_args_list[1].kwargs["tool_choice"], {"type": "function", "name": "get_issue_comments"})
        self.assertEqual(result.comment_check.status, "read")
        self.assertEqual(create.call_args_list[4].kwargs["tool_choice"], {"type": "function", "name": "get_related_issue"})
        self.assertEqual(create.call_args_list[5].kwargs["tool_choice"], "none")

    async def test_two_successful_distinct_empty_queries_are_not_an_unread_check(self):
        result, create, _ = await run_policy([
            *prefix(), step("search_issues", query="export"), simulated_response(content=final_content()),
        ], PolicyServer(empty=True))
        self.assertIsNotNone(result.content)
        self.assertEqual(create.call_args_list[3].kwargs["tool_choice"], {"type": "function", "name": "search_issues"})
        self.assertEqual(result.similar_issue_check.search_status, "empty")
        self.assertEqual(result.similar_issue_check.candidate_status, "not_needed")
        self.assertEqual(result.similar_issue_check.comparison_status, "not_needed")
        self.assertEqual(len(result.similar_issue_check.attempted_queries), 2)
        self.assertEqual(result.content.comparisons, [])
        self.assertTrue(functional_checks(result)["minimum_similar_issue_checks_completed"])

    async def test_cached_same_query_does_not_count_as_the_broader_search(self):
        server = PolicyServer(empty=True)
        result, _, _ = await run_policy([
            *prefix(), step("search_issues", query="EXPORT crash"), step("search_issues", query="export"),
            simulated_response(content=final_content()),
        ], server)
        self.assertIsNotNone(result.content)
        self.assertEqual(sum(request.url.path == "/search/issues" for request in server.requests), 2)
        self.assertEqual(result.similar_issue_check.attempted_queries, ["export crash", "export"])
        self.assertTrue(any("cached repeat" in limitation for limitation in result.limitations))

    async def test_transient_required_search_retries_within_the_same_tool_call(self):
        server = PolicyServer(empty=True)
        failed = False

        def respond(request):
            nonlocal failed
            if request.url.path == "/search/issues" and not failed:
                failed = True
                server.requests.append(request)
                return httpx.Response(503)
            return server.respond(request)

        responses = [*prefix(), step("search_issues", query="export"), simulated_response(content=final_content())]
        first_search_id = responses[2].output[0].call_id
        with patch.object(AgentRun, "_wait_for_retry", new=AsyncMock()):
            result, create, emit = await run_simulated_agent(responses, handler=respond, required_lookups=True)
        state = result.similar_issue_check
        self.assertEqual(state.attempted_queries, ["export crash", "export"])
        self.assertEqual((state.search_status, state.failed_queries), ("empty", []))
        self.assertEqual(result.limitations, [])
        self.assertEqual(result.usage.tool_calls, 5)
        self.assertEqual(sum(request.url.path == "/search/issues" for request in server.requests), 3)
        outputs = [item for item in create.call_args.kwargs["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(sum(item["call_id"] == first_search_id for item in outputs), 1)
        self.assertTrue(any("retry for search_issues succeeded" in item.args[1] for item in emit.call_args_list))

    async def test_failed_search_is_partial_not_successful_absence_and_is_not_forced_forever(self):
        server = PolicyServer()
        server.search_status = 500
        result, create, _ = await run_policy([
            *prefix(), simulated_response(content=final_content()),
        ], server)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.similar_issue_check.search_status, "failed")
        self.assertEqual(result.similar_issue_check.failed_queries, ["export crash"])
        self.assertNotEqual(result.similar_issue_check.comparison_status, "not_needed")
        self.assertEqual(create.call_args.kwargs["tool_choice"], "auto")
        self.assertEqual(sum(request.url.path == "/search/issues" for request in server.requests), 2)
        self.assertFalse(functional_checks(result)["minimum_similar_issue_checks_completed"])
        self.assertTrue(any("the one retry was exhausted" in item for item in result.limitations))

    async def test_failed_broader_query_does_not_become_a_completed_empty_check(self):
        server = PolicyServer(empty=True)
        original = server.respond

        def respond(request):
            if request.url.path == "/search/issues" and request.url.params["q"].endswith('"export"'):
                server.search_status = 500
            return original(request)

        result, _, _ = await run_simulated_agent([
            *prefix(), step("search_issues", query="export"), simulated_response(content=final_content()),
        ], handler=respond, required_lookups=True)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.similar_issue_check.search_status, "partial")
        self.assertEqual(result.similar_issue_check.failed_queries, ["export"])
        self.assertNotEqual(result.similar_issue_check.candidate_status, "not_needed")

    async def test_malformed_successful_search_data_is_a_failed_lookup(self):
        for data in ({}, [], {"total_count": 1, "items": [{**CANDIDATE, "title": None}]}):
            with self.subTest(data=data):
                server = PolicyServer()
                server.malformed_search = data
                result, _, _ = await run_policy([*prefix(), simulated_response(content=final_content())], server)
                self.assertEqual(result.similar_issue_check.search_status, "failed")
                self.assertEqual(result.candidates, [])

    async def test_failed_candidate_is_not_an_unread_success_or_a_comparison(self):
        server = PolicyServer()
        server.candidate_statuses = [500]
        result, create, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=final_content()),
        ], server, replace(SETTINGS, max_candidate_reads=1))
        self.assertIsNotNone(result.content)
        self.assertEqual(result.similar_issue_check.candidate_status, "failed")
        self.assertEqual(result.similar_issue_check.failed_candidate_numbers, [41])
        self.assertEqual(result.similar_issue_check.read_candidate_numbers, [])
        self.assertEqual(result.similar_issue_check.comparison_status, "unavailable")
        self.assertEqual(result.content.comparisons, [])
        self.assertEqual(server.candidate_calls, 1)
        self.assertTrue(any("candidate-read budget was exhausted" in item for item in result.limitations))
        self.assertEqual(create.call_args.kwargs["tool_choice"], "auto")

    async def test_malformed_candidate_body_cannot_satisfy_the_read_gate(self):
        server = PolicyServer()
        server.malformed_candidate = True
        result, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=final_content()),
        ], server)
        self.assertEqual(result.similar_issue_check.candidate_status, "failed")
        self.assertIsNone(result.candidates[0].source_id)

    async def test_optional_retry_can_recover_failed_candidate_state(self):
        server = PolicyServer()
        server.candidate_statuses = [404, 200]
        result, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], server)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.similar_issue_check.failed_candidate_numbers, [])
        self.assertEqual(result.similar_issue_check.candidate_status, "read")
        self.assertEqual(result.similar_issue_check.comparison_status, "compared")
        self.assertEqual(server.candidate_calls, 2)
        self.assertTrue(result.limitations)

    async def test_catalog_failure_is_attempted_once_then_other_checks_continue(self):
        server = PolicyServer()
        server.labels_status = 403
        result, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison("S3")),
        ], server)
        self.assertIsNotNone(result.content)
        self.assertIsNone(result.labels_source_id)
        self.assertEqual(sum(request.url.path.endswith("/labels") for request in server.requests), 1)
        self.assertEqual(result.similar_issue_check.comparison_status, "compared")
        self.assertTrue(result.limitations)

    async def test_wrong_tool_is_rejected_before_network_and_does_not_skip_a_gate(self):
        server = PolicyServer()
        result, create, _ = await run_policy([
            step("get_issue"), step("get_repo_readme"), step("get_repo_labels"),
            step("search_issues", query="export"), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], server)
        self.assertIsNotNone(result.content)
        self.assertFalse(any(request.url.path.endswith("/readme") for request in server.requests))
        self.assertEqual(create.call_args_list[2].kwargs["tool_choice"], {"type": "function", "name": "get_repo_labels"})

    async def test_target_number_does_not_count_as_a_candidate_attempt(self):
        server = PolicyServer()
        result, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=42), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], server)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.similar_issue_check.attempted_candidate_numbers, [41])
        self.assertEqual(sum(request.url.path.endswith("/issues/42") for request in server.requests), 1)

    async def test_premature_final_is_retained_but_mandatory_tools_still_execute(self):
        result, create, _ = await run_policy([
            step("get_issue"), simulated_response(content=final_content()), step("get_repo_labels"),
            step("search_issues", query="export"), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        self.assertIsNotNone(result.content)
        self.assertIn("Complete get_repo_labels", result.rejected_answers[0].reason)
        self.assertEqual(create.call_args_list[2].kwargs["tool_choice"], {"type": "function", "name": "get_repo_labels"})
        self.assertEqual(result.similar_issue_check.comparison_status, "compared")

    async def test_retrieved_body_requires_a_comparison_before_acceptance(self):
        result, create, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=final_content()),
            simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        self.assertIsNotNone(result.content)
        self.assertIn("missing: [41]", result.rejected_answers[0].reason)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(create.call_args_list[4].kwargs["input"], create.call_args_list[5].kwargs["input"])

    async def test_iteration_limit_preserves_unattempted_search_and_unread_candidate_states(self):
        for limit, responses, search_status, candidate_status in (
            (2, [step("get_issue"), simulated_response(content=final_content())], "not_checked", "not_checked"),
            (4, [*prefix(), simulated_response(content=final_content())], "candidates_found", "not_checked"),
        ):
            with self.subTest(limit=limit):
                result, create, _ = await run_policy(responses, PolicyServer(), replace(SETTINGS, max_iterations=limit))
                self.assertIsNotNone(result.content)
                self.assertEqual(create.await_count, limit)
                self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
                self.assertEqual(result.similar_issue_check.search_status, search_status)
                self.assertEqual(result.similar_issue_check.candidate_status, candidate_status)
                self.assertNotEqual(result.similar_issue_check.comparison_status, "not_needed")
                self.assertTrue(result.limitations)

    async def test_tool_limit_does_not_trigger_extra_lookups(self):
        result, _, _ = await run_policy([
            step("get_issue"), step("get_repo_labels"), simulated_response(content=final_content()),
        ], PolicyServer(), replace(SETTINGS, max_tool_calls=2))
        self.assertIsNotNone(result.content)
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual(result.similar_issue_check.search_status, "not_checked")

    async def test_first_empty_query_is_partial_when_no_broader_query_fits_the_request_limit(self):
        result, _, _ = await run_policy([
            *prefix(), simulated_response(content=final_content()),
        ], PolicyServer(empty=True), replace(SETTINGS, max_iterations=4))
        self.assertIsNotNone(result.content)
        self.assertEqual(result.similar_issue_check.search_status, "partial")
        self.assertNotEqual(result.similar_issue_check.candidate_status, "not_needed")
        self.assertTrue(result.limitations)

    async def test_context_fallback_keeps_unattempted_checks_explicit(self):
        probe, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        budget = probe.context_builds[1].estimated_tokens + 8
        result, create, _ = await run_policy([
            step("get_issue"), simulated_response(content=final_content()),
        ], PolicyServer(), replace(SETTINGS, context_tokens=budget))
        self.assertIsNotNone(result.content)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(result.similar_issue_check.search_status, "not_checked")
        self.assertEqual(result.similar_issue_check.attempted_queries, [])
        self.assertTrue(any("without a full repair reserve" in item for item in result.limitations))

    async def test_cached_body_read_preserves_one_actual_retrieval_and_a_grounded_comparison(self):
        server = PolicyServer()
        result, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], server)
        self.assertIsNotNone(result.content)
        self.assertEqual(server.candidate_calls, 1)
        self.assertEqual(result.similar_issue_check.attempted_candidate_numbers, [41])
        self.assertEqual(result.similar_issue_check.comparison_status, "compared")
        self.assertTrue(result.context_builds[-1].reused_call_ids)


class FutureLookupReserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserve_releases_as_catalog_search_and_candidate_arrive(self):
        result, create, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        self.assertIsNotNone(result.content)
        base = CONTINUATION_RESERVE_TOKENS
        body = CANDIDATE_RESERVE_TOKENS
        lookup = PENDING_LOOKUP_RESERVE_TOKENS
        self.assertEqual([report.future_reserve_tokens for report in result.context_builds], [
            base, base + body + 2 * lookup, base + body + lookup, base + body, base,
        ])
        self.assertTrue(all(call.kwargs["max_output_tokens"] == 2400 for call in create.call_args_list))

    async def test_second_empty_search_or_failed_candidate_releases_body_reserve(self):
        for empty, failed in ((True, False), (False, True)):
            with self.subTest(empty=empty, failed=failed):
                server = PolicyServer(empty=empty)
                if failed:
                    server.candidate_statuses = [500]
                followup = step("search_issues", query="export") if empty else step("get_related_issue", issue_number=41)
                result, _, _ = await run_policy([
                    *prefix(), followup, simulated_response(content=final_content()),
                ], server)
                self.assertIsNotNone(result.content)
                self.assertGreater(result.context_builds[-2].future_reserve_tokens, CONTINUATION_RESERVE_TOKENS)
                self.assertEqual(result.context_builds[-1].future_reserve_tokens, CONTINUATION_RESERVE_TOKENS)
                self.assertEqual(result.similar_issue_check.candidate_status, "not_needed" if empty else "failed")

    async def test_no_room_for_future_steps_finalizes_with_explicit_unread_state(self):
        probe, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        budget = probe.context_builds[1].estimated_tokens + 8
        result, create, emit = await run_policy([
            step("get_issue"), simulated_response(content=final_content()),
        ], PolicyServer(), replace(SETTINGS, context_tokens=budget))
        self.assertIsNotNone(result.content)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(result.similar_issue_check.search_status, "not_checked")
        self.assertIsNone(result.labels_source_id)
        self.assertTrue(any("Finalizing early" in item for item in result.limitations))
        self.assertTrue(any("Finalizing early" in call.args[1] for call in emit.call_args_list))
        self.assertFalse(result.context_builds[-2].sent)
        self.assertTrue(any(not item.fits and item.future_reserve_target_tokens > 0 for item in result.context_builds))
        self.assertEqual(result.context_builds[-1].future_reserve_tokens, 0)

    async def test_context_finalization_does_not_execute_an_unexpected_tool(self):
        probe, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        server = PolicyServer()
        result, create, _ = await run_policy([
            step("get_issue"), step("get_repo_labels"),
        ], server, replace(SETTINGS, context_tokens=probe.context_builds[1].estimated_tokens + 8))
        self.assertIsNone(result.content)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(len(server.requests), 1)
        self.assertTrue(any("no tool was executed" in item for item in result.limitations))

    async def test_candidate_is_not_fetched_when_its_minimum_reserve_cannot_fit(self):
        probe, _, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        budget = probe.context_builds[3].estimated_tokens + CANDIDATE_RESERVE_TOKENS - 100
        server = PolicyServer()
        result, create, _ = await run_policy([
            *prefix(), simulated_response(content=final_content()),
        ], server, replace(SETTINGS, context_tokens=budget))
        self.assertIsNotNone(result.content)
        self.assertEqual(server.candidate_calls, 0)
        self.assertEqual(result.similar_issue_check.candidate_status, "not_checked")
        self.assertEqual(result.similar_issue_check.attempted_candidate_numbers, [])
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(result.context_builds[-2].future_reserve_minimum_tokens, CANDIDATE_RESERVE_TOKENS)
        self.assertEqual(result.context_builds[-1].repair_reserve_tokens, 256)
        self.assertTrue(any("pending checks remain incomplete" in item for item in result.limitations))

    async def test_long_comments_no_longer_block_the_later_candidate_at_the_same_budget(self):
        class LargeEvidenceServer(PolicyServer):
            def respond(self, request):
                if request.url.path.endswith("/comments"):
                    return httpx.Response(200, json=[{
                        **COMMENT, "id": 101 + index, "author_association": "NONE",
                        "body": "Unrelated project background. " * 50,
                    } for index in range(8)])
                response = super().respond(request)
                if request.url.path.endswith("/issues/42"):
                    return httpx.Response(200, json={**response.json(), "body": "Version 1.4 on Windows. Export closes the app. " * 80})
                if request.url.path.endswith("/issues/41"):
                    return httpx.Response(200, json={**response.json(), "body": "Version 1.3 on Linux. Export closes the app. " * 80})
                return response

        responses = [
            step("get_issue"), step("get_issue_comments", page=1), step("get_repo_labels"),
            step("search_issues", query="export crash"), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison("S12")),
        ]
        with patch.object(AgentRun, "future_context_reserve", return_value=0):
            probe, _, _ = await run_policy(responses, LargeEvidenceServer(comments=8))
            self.assertIsNotNone(probe.content)
            settings = replace(SETTINGS, context_tokens=probe.context_builds[-1].estimated_tokens - 128, model="gpt-5.6", reasoning_effort="medium")
            old, _, _ = await run_policy(responses, LargeEvidenceServer(comments=8), settings)
        self.assertIsNone(old.content)
        self.assertEqual(old.similar_issue_check.read_candidate_numbers, [41])
        self.assertFalse(old.context_builds[-1].sent)

        result, create, _ = await run_policy(responses, LargeEvidenceServer(comments=8), settings)
        self.assertIsNotNone(result.content, {
            "builds": [(item.request_number, item.estimated_tokens, item.future_reserve_tokens, item.fits, item.sent) for item in result.context_builds],
            "limitations": result.limitations,
        })
        self.assertEqual(result.similar_issue_check.compared_candidate_numbers, [41])
        self.assertEqual(create.await_count, 6)
        self.assertEqual(result.usage.tool_calls, 5)
        omitted = [item.source_id for report in result.context_builds for item in report.sources if item.action == "omitted"]
        self.assertTrue(omitted)
        self.assertTrue(all(next(source for source in result.sources if source.id == source_id).excerpt for source_id in omitted))
        self.assertEqual(result.comment_check.retrieved_count, 8)
        self.assertEqual(result.comment_check.status, "read")
        self.assertTrue(result.comment_check.complete)
        self.assertTrue(functional_checks(result)["dispatched_contexts_within_budget"])
        for earlier, later in zip(create.call_args_list, create.call_args_list[1:]):
            prefix_input = earlier.kwargs["input"]
            self.assertEqual(later.kwargs["input"][:len(prefix_input)], prefix_input)
        self.assertEqual(result.sources[-1].kind, "related_issue")
        self.assertTrue(result.sources[-1].excerpt)


class RequiredLookupAPITests(unittest.TestCase):
    def test_attempt_read_and_comparison_states_survive_job_serialization(self):
        result, _, _ = asyncio.run(run_policy([
            *prefix(), step("get_related_issue", issue_number=41), simulated_response(content=content_with_comparison()),
        ], PolicyServer()))
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=result)):
            with TestClient(create_app(SETTINGS)) as client:
                created = client.post("/api/v1/analyses", json={"issue_url": TARGET.url})
                job_id = created.json()["job_id"]
                stream = client.get(f"/api/v1/analyses/{job_id}/events")
                self.assertIn("event: done", stream.text)
                snapshot = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(snapshot["status"], "completed")
                self.assertEqual(snapshot["result"]["similar_issue_check"]["comparison_status"], "compared")
                self.assertEqual(snapshot["result"]["similar_issue_check"]["compared_candidate_numbers"], [41])


if __name__ == "__main__":
    unittest.main()
