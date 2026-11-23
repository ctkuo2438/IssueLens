"""What each model request may do, decided from the evidence so far, without a model or GitHub."""

from dataclasses import replace
import unittest
from unittest.mock import patch

import httpx

from backend.app.agent import CANDIDATE_RESERVE_TOKENS, CONTINUATION_RESERVE_TOKENS, AgentRun
from backend.app.checkpoint import AgentCheckpoint
from backend.app.github import GitHubTools
from backend.app.models import IssueCandidate, IssueInfo, IssueSearch
from backend.app.trace import TraceLog
from test_mvp import (
    SETTINGS, TARGET, OfflineEncoding, final_content, run_simulated_agent, simulated_response, tool_call,
)


def run_after_reading_the_issue(*, comments: int = 0) -> AgentRun:
    """A run whose server reads found the target issue and the label catalog, with no other lookup."""
    run = AgentRun(TARGET, SETTINGS, lambda *_: None)
    run.tools = GitHubTools(httpx.AsyncClient(), TARGET, lambda text, limit: (text, False))
    run.tools.issue = IssueInfo(
        title="Export fails on Windows", url=TARGET.url, number=42, state="open", author="reporter",
        labels=[], comment_count=comments,
    )
    run.tools.label_lookup_attempted = True
    run.tools.labels_source_id = "S2"
    return run


def run_with_core_checks() -> AgentRun:
    """A run with the label catalog and two empty searches, so no lookup is required."""
    run = run_after_reading_the_issue()
    assert run.tools is not None
    run.tools.search_attempts = ["export", "export windows"]
    run.tools.successful_search_queries = {"export", "export windows"}
    run.tools.searches = [
        IssueSearch(query=query, total_count=0, incomplete_results=False, candidate_numbers=[], source_id=source_id)
        for query, source_id in (("export", "S3"), ("export windows", "S4"))
    ]
    return run


class RequestPlanTests(unittest.TestCase):
    def test_a_required_lookup_is_the_only_tool_offered(self):
        # Comments are the server's read, so an unread page never becomes the model's required lookup.
        plan = run_after_reading_the_issue(comments=2).plan_request(1, None)
        self.assertEqual(plan.required_action, "search_issues")
        self.assertEqual(plan.tool_choice, {"type": "function", "name": "search_issues"})
        self.assertIn("The required next action is search_issues.", plan.instructions)

    def test_the_last_request_closes_tools_even_with_lookups_pending(self):
        plan = run_after_reading_the_issue(comments=2).plan_request(SETTINGS.max_iterations, None)
        self.assertTrue(plan.at_limit)
        self.assertEqual((plan.tool_choice, plan.required_action, plan.action_instruction), ("none", None, ""))
        self.assertEqual(plan.future_reserve, 0)

    def test_a_repair_closes_tools_and_carries_the_feedback(self):
        plan = run_after_reading_the_issue().plan_request(3, "Cite S1.")
        self.assertTrue(plan.repairing)
        self.assertEqual((plan.tool_choice, plan.required_action), ("none", None))
        self.assertIn("Final-answer repair feedback: Cite S1.", plan.instructions)
        self.assertEqual(plan.future_reserve, 0)

    def test_core_checks_near_the_end_leave_requests_for_repair(self):
        run = run_with_core_checks()
        last_tool_request = run.plan_request(SETTINGS.max_iterations - 3, None)
        self.assertEqual((last_tool_request.closing_for_repairs, last_tool_request.tool_choice), (False, "auto"))
        closing = run.plan_request(SETTINGS.max_iterations - 2, None)
        self.assertEqual((closing.closing_for_repairs, closing.tool_choice), (True, "none"))
        self.assertIn("Return final JSON now", closing.instructions)

    def test_a_required_candidate_read_reserves_room_for_its_body(self):
        run = run_with_core_checks()
        assert run.tools is not None
        run.tools.search_attempts = ["export"]
        run.tools.candidates = {41: IssueCandidate(number=41, title="Export crash", url="https://example.com", state="open")}
        plan = run.plan_request(3, None)
        self.assertEqual(plan.required_action, "get_related_issue")
        self.assertEqual(plan.minimum_future_reserve, CANDIDATE_RESERVE_TOKENS)
        self.assertEqual(plan.future_reserve, CONTINUATION_RESERVE_TOKENS + CANDIDATE_RESERVE_TOKENS)

    def test_planning_changes_nothing(self):
        run = run_after_reading_the_issue(comments=2)
        assert run.tools is not None
        before = (run.tools.checkpoint(), run.result.model_copy(deep=True))
        self.assertEqual(run.plan_request(2, None), run.plan_request(2, None))
        self.assertEqual((run.tools.checkpoint(), run.result), before)


class LoopEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_checkpoint_for_another_issue_is_refused_before_any_request(self):
        checkpoint = AgentCheckpoint.start("fixture")
        checkpoint.history = [{
            "role": "user",
            "content": "Investigate this GitHub issue and draft a maintainer reply: https://github.com/example/project/issues/7",
        }]
        with (
            patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
            patch("backend.app.agent.AsyncOpenAI") as client,
            self.assertRaisesRegex(ValueError, "does not belong to the requested issue"),
        ):
            await AgentRun(TARGET, SETTINGS, lambda *_: None, checkpoint=checkpoint).run()
        client.assert_not_called()

    async def test_a_tool_requested_after_tools_close_is_not_run(self):
        trace = TraceLog()
        result, create, _ = await run_simulated_agent([
            simulated_response([tool_call("get_repo_readme", call_id="call_offline_2")]),
            simulated_response(content=final_content()),
        ], replace(SETTINGS, max_tool_calls=1), trace=trace)
        # The server's issue read used the only tool call.
        self.assertEqual(create.call_args_list[0].kwargs["tool_choice"], "none")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertIn("tool-call budget was reached", create.call_args_list[1].kwargs["input"][-1]["output"])
        self.assertEqual([attempt.kind for attempt in trace.attempts], ["tool", "model", "model"])
        rejected = trace.attempts[1]
        self.assertEqual(
            (rejected.kind, rejected.outcome, rejected.reason, rejected.stop_reason),
            ("model", "rejected", "The response requested a tool while tool use was closed.", None),
        )
        self.assertIsNotNone(result.content)
