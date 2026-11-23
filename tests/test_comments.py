"""Offline comment retrieval, policy and claim checks with simulated providers."""

import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx

from backend.app.github import GitHubTools, ToolError
from backend.app.main import create_app
from backend.app.models import AnalysisContent, CommentCheck
from backend.app.quality import comment_claim_fields
from test_mvp import COMMENT, ISSUE, SETTINGS, TARGET, commented_github_response, final_content, run_simulated_agent, simulated_response


class CommentServer:
    def __init__(self, count=1):
        self.count = count
        self.pages = {1: ([COMMENT], False)}
        self.fail_page = None
        self.requests = []

    def respond(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/comments"):
            page = int(request.url.params["page"])
            if page == self.fail_page:
                return httpx.Response(403, json={"message": "Untrusted upstream error."})
            data, more = self.pages.get(page, ([], False))
            headers = {"Link": f'<https://api.github.com{TARGET.api_path}/issues/42/comments?page={page + 1}>; rel="next"'} if more else {}
            return httpx.Response(200, json=data, headers=headers)
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={**ISSUE, "comments": self.count})


class CommentToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = CommentServer()
        self.client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(self.server.respond))
        self.tools = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_zero_metadata_is_unread_until_a_successful_empty_lookup(self):
        self.server.count = 0
        self.server.pages[1] = ([], False)
        target = await self.tools.execute("get_issue", "{}")
        self.assertEqual(target["comment_check"]["status"], "not_read")
        self.assertFalse(target["comment_check"]["complete"])
        page = await self.tools.execute("get_issue_comments", '{"page":1}')
        self.assertEqual(page["comment_check"]["status"], "empty")
        self.assertTrue(page["comment_check"]["complete"])

    async def test_read_and_cached_outputs_report_current_coverage(self):
        self.server.count = 2
        self.server.pages = {1: ([COMMENT], True), 2: ([{**COMMENT, "id": 102}], False)}
        await self.tools.execute("get_issue", "{}")
        first = await self.tools.execute("get_issue_comments", '{"page":1}')
        self.assertEqual(first["comment_check"]["status"], "partial")
        self.assertTrue(first["has_next_page"])
        await self.tools.execute("get_issue_comments", '{"page":2}')
        cached = await self.tools.execute("get_issue_comments", '{"page":1}')
        target = await self.tools.execute("get_issue", "{}")
        self.assertEqual(cached["comment_check"]["status"], "read")
        self.assertEqual(target["comment_check"]["read_pages"], [1, 2])
        self.assertEqual(target["comment_check"]["retrieved_count"], 2)
        self.assertEqual(first["comment_check"]["status"], "partial")
        self.assertEqual(len(self.server.requests), 3)

    async def test_failed_page_is_not_empty_and_successful_retry_clears_failed_state(self):
        await self.tools.execute("get_issue", "{}")
        self.server.fail_page = 1
        with self.assertRaises(ToolError):
            await self.tools.execute("get_issue_comments", '{"page":1}')
        self.assertEqual(self.tools.comment_check.status, "failed")
        self.assertEqual(self.tools.comment_check.failed_pages, [1])
        self.assertEqual(self.tools.comment_check.read_pages, [])
        self.server.fail_page = None
        await self.tools.execute("get_issue_comments", '{"page":1}')
        self.assertEqual(self.tools.comment_check.status, "read")
        self.assertEqual(self.tools.comment_check.failed_pages, [])

    async def test_empty_later_page_does_not_establish_absence(self):
        self.server.count = 0
        await self.tools.execute("get_issue", "{}")
        await self.tools.execute("get_issue_comments", '{"page":2}')
        self.assertEqual(self.tools.comment_check.status, "partial")
        self.assertEqual(self.tools.comment_check.read_pages, [2])
        self.assertFalse(self.tools.comment_check.complete)

    async def test_metadata_count_disagreement_is_partial(self):
        self.server.count = 2
        self.server.pages[1] = ([], False)
        await self.tools.execute("get_issue", "{}")
        await self.tools.execute("get_issue_comments", '{"page":1}')
        self.assertEqual(self.tools.comment_check.status, "partial")
        self.assertTrue(self.tools.comment_check.count_mismatch)

    async def test_malformed_successful_payload_is_a_failure_not_a_successful_empty_check(self):
        for data in ({}, "invalid", [None], [{"id": "invalid"}]):
            with self.subTest(data=data):
                self.server.pages[1] = (data, False)
                with self.assertRaises(ToolError):
                    await self.tools.execute("get_issue_comments", '{"page":1}')
                self.assertEqual(self.tools.comment_check.status, "failed")
                self.assertFalse(self.tools.comment_check.complete)
                self.assertEqual(self.tools.sources, {})

    async def test_page_coverage_and_excerpt_truncation_remain_distinct(self):
        await self.tools.execute("get_issue", "{}")
        self.tools.clip = lambda text, budget: (text[:5], True)
        await self.tools.execute("get_issue_comments", '{"page":1}')
        self.assertEqual(self.tools.comment_check.status, "read")
        self.assertTrue(self.tools.comment_check.complete)
        self.assertTrue(any("truncated" in item for item in self.tools.limitations))
        self.assertEqual(self.tools.comment_check.source_ids, ["S2"])


class CommentClaimTests(unittest.TestCase):
    def content(self, text):
        content = AnalysisContent.model_validate(final_content())
        content.summary.text = text
        return content

    def test_known_absence_claims_are_rejected_for_unread_failed_partial_and_nonempty_reads(self):
        for state in ("not_read", "failed", "partial", "read"):
            for text in ("No discussion comments were available to change the picture.", "The issue has no comments.", "Comments were unavailable."):
                with self.subTest(state=state, text=text):
                    self.assertEqual(comment_claim_fields(self.content(text), CommentCheck(status=state, reported_count=2)), ["summary"])

    def test_empty_lookup_and_zero_metadata_support_scoped_absence(self):
        content = self.content("No comments were found.")
        self.assertEqual(comment_claim_fields(content, CommentCheck(status="empty", complete=True)), [])
        self.assertEqual(comment_claim_fields(content, CommentCheck(reported_count=0)), [])

    def test_truthful_statuses_and_negated_complete_read_claims_are_allowed(self):
        for text in ("The comments were not read.", "The comments lookup failed; existence is unknown.", "Only page 1 was retrieved.", "Not all comments were reviewed.", "I have not yet reviewed all comments."):
            with self.subTest(text=text):
                self.assertEqual(comment_claim_fields(self.content(text), CommentCheck(status="partial", reported_count=12)), [])
        self.assertEqual(comment_claim_fields(self.content("All comments were reviewed."), CommentCheck(status="partial")), ["summary"])

    def test_retrieved_but_omitted_excerpts_do_not_support_a_complete_review_claim(self):
        check = CommentCheck(status="read", complete=True, reported_count=2, retrieved_count=2, source_ids=["S2", "S3"])
        content = self.content("All comments were reviewed.")
        self.assertEqual(comment_claim_fields(content, check, visible_source_ids={"S2"}), ["summary"])
        self.assertEqual(comment_claim_fields(content, check, visible_source_ids={"S2", "S3"}), [])


class CommentAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_positive_count_reads_page_one_before_the_first_request(self):
        server = CommentServer()
        result, create, emit = await run_simulated_agent([simulated_response(content=final_content())], handler=server.respond)
        pages = [req.url.params["page"] for req in server.requests if req.url.path.endswith("/comments")]
        self.assertEqual(pages, ["1"])
        self.assertEqual(create.await_count, 1)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "auto")
        self.assertEqual(result.comment_check.status, "read")
        self.assertTrue(any("Comment check: read" in call.args[1] for call in emit.call_args_list))

    async def test_zero_count_skips_the_comment_lookup(self):
        server = CommentServer(count=0)
        result, _, _ = await run_simulated_agent([simulated_response(content=final_content())], handler=server.respond)
        self.assertFalse(any(req.url.path.endswith("/comments") for req in server.requests))
        self.assertEqual(result.comment_check.status, "not_read")
        self.assertFalse(any("comments" in item for item in result.limitations))

    async def test_failed_lookup_requests_bounded_correction_for_false_absence(self):
        server = CommentServer()
        server.fail_page = 1
        bad = final_content()
        bad["summary"]["text"] = "No discussion comments were available."
        corrected = final_content()
        corrected["summary"]["text"] = "The comment lookup failed, so the discussion was not reviewed."
        result, create, emit = await run_simulated_agent([
            simulated_response(content=bad), simulated_response(content=corrected),
        ], handler=server.respond)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.comment_check.status, "failed")
        self.assertEqual(create.call_args_list[0].kwargs["tool_choice"], "auto")
        self.assertEqual(len([req for req in server.requests if req.url.path.endswith("/comments")]), 1)
        self.assertTrue(any("Unsupported comment absence" in call.args[1] for call in emit.call_args_list))
        output = next(
            item for item in create.call_args_list[0].kwargs["input"]
            if item.get("type") == "function_call_output" and item["call_id"] == "server_get_issue_comments"
        )
        self.assertEqual(json.loads(output["output"])["comment_check"]["status"], "failed")

    async def test_tool_budget_exhaustion_preserves_unread_state_without_exceeding_limits(self):
        result, create, _ = await run_simulated_agent(
            [simulated_response(content=final_content())],
            settings=replace(SETTINGS, max_tool_calls=1), handler=commented_github_response,
        )
        self.assertIsNotNone(result.content)
        self.assertEqual(result.comment_check.status, "not_read")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertTrue(any("no comments lookup" in item for item in result.limitations))

    async def test_false_absence_is_rejected_when_no_correction_budget_remains(self):
        bad = final_content()
        bad["draft_reply"] = "No discussion comments were available."
        result, _, emit = await run_simulated_agent(
            [simulated_response(content=bad)],
            settings=replace(SETTINGS, max_iterations=1, max_tool_calls=1), handler=commented_github_response,
        )
        self.assertIsNone(result.content)
        self.assertEqual(result.comment_check.status, "not_read")
        self.assertTrue(any("No correction budget remains" in call.args[1] for call in emit.call_args_list))


class CommentAPITests(unittest.TestCase):
    def test_comment_state_survives_job_snapshot_and_event_stream(self):
        result, _, _ = asyncio.run(run_simulated_agent(
            [simulated_response(content=final_content())], handler=commented_github_response,
        ))
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=result)):
            with TestClient(create_app(SETTINGS)) as client:
                job_id = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                stream = client.get(f"/api/v1/analyses/{job_id}/events")
                snapshot = client.get(f"/api/v1/analyses/{job_id}").json()
                self.assertEqual(snapshot["result"]["comment_check"]["status"], "read")
                self.assertEqual(snapshot["result"]["comment_check"]["retrieved_count"], 1)
                self.assertIn('"comment_check"', stream.text)
