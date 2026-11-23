"""Offline temporal metadata and narrow draft checks with simulated providers."""

from copy import deepcopy
from datetime import UTC, datetime
import json
import unittest

import httpx

from backend.app.github import GitHubTools
from backend.app.models import AnalysisContent, AnalysisResult
from backend.app.quality import draft_temporal_claims
from evaluation.checks import check_result
from scripts.acceptance import functional_checks
from test_evaluation import DATASET
from test_labels import label_content, label_github_response, label_prefix
from test_mvp import COMMENT, ISSUE, TARGET, final_content, run_simulated_agent, simulated_response, tool_call


CREATED = "2020-01-01T12:00:00Z"
CLOSED = "2020-01-04T12:00:00Z"
COMMENTS = [
    {**COMMENT, "created_at": "2020-01-02T12:00:00Z", "body": "I expect the cleanup timing fix to land in version 2.0."},
    {**COMMENT, "id": 102, "user": {"login": "reporter"}, "created_at": "2020-01-03T12:00:00Z", "author_association": "NONE", "body": "Our cleanup is already safe to repeat. The problem is when it runs, not how many times it runs."},
]


def draft_github_response(request):
    if request.url.path.endswith("/comments"):
        return httpx.Response(200, json=COMMENTS)
    if request.url.path.endswith("/labels"):
        return httpx.Response(200, json=[])
    return httpx.Response(200, json={**ISSUE, "state": "closed", "comments": 2, "created_at": CREATED, "closed_at": CLOSED})


def draft_content(text):
    content = final_content()
    content["draft_reply"] = text
    content["missing_information"] = []
    return content


class DraftTemporalRuleTests(unittest.TestCase):
    def test_known_unattributed_fix_forecasts_are_flagged(self):
        for text in (
            "This will be fixed in version 2.0.",
            "That will be addressed as part of #100 in version 2.0.",
            "This is expected to be addressed in version 2.0.",
            "The fix will land in version 2.0.",
            "We will fix this in the next release.",
            "This should be corrected as part of #100 in version 2.0.",
        ):
            with self.subTest(text=text):
                self.assertIn("unattributed_fix_forecast", draft_temporal_claims(text))

    def test_reported_historical_plans_and_documentation_claims_are_allowed(self):
        for text in (
            "In the 2020 discussion, a maintainer expected a fix in version 2.0; current release status was not checked.",
            "A maintainer said in 2020 that this will be fixed in version 2.0.",
            "The maintainers stated that the fix will land in version 2.0.",
            "According to the reporter, the project does not support this option.",
            "The README states that the library does not currently expose this API.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_temporal_claims(text), [])

    def test_known_present_support_and_release_assertions_are_flagged(self):
        for text, expected in (
            ("The library does not currently expose the requested API.", "unattributed_current_capability"),
            ("The project doesn't support this option yet.", "unattributed_current_capability"),
            ("This is fixed in version 2.0.", "unattributed_release_claim"),
            ("The problem has been resolved in v2.0.", "unattributed_release_claim"),
        ):
            with self.subTest(text=text):
                self.assertIn(expected, draft_temporal_claims(text))

    def test_questions_uncertainty_and_concrete_next_steps_remain_allowed(self):
        for text in (
            "I cannot confirm that it will be fixed in version 2.0.",
            "Could you confirm whether it will be fixed in version 2.0?",
            "The example does not implement a complete route.",
            "Please do not implement a workaround that hides this error.",
            "A minimal application would help investigate the remaining unknowns.",
            "This should be fixed before proposing a broader refactor.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_temporal_claims(text), [])

    def test_rule_does_not_rewrite_draft_or_source_text(self):
        draft = "This will be fixed in version 2.0."
        sources = deepcopy(COMMENTS)
        self.assertTrue(draft_temporal_claims(draft))
        self.assertEqual(draft, "This will be fixed in version 2.0.")
        self.assertEqual(COMMENTS, sources)


class DraftMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_issue_dates_are_validated_and_returned_as_json_values(self):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(draft_github_response)) as client:
            tools = GitHubTools(client, TARGET, lambda text, limit: (text, False))
            result = await tools.execute("get_issue", "{}")
            self.assertEqual(result["issue"]["created_at"], CREATED)
            self.assertEqual(result["issue"]["closed_at"], CLOSED)
            self.assertEqual(tools.issue.created_at, datetime(2020, 1, 1, 12, tzinfo=UTC))
            self.assertEqual(json.loads(json.dumps(result))["issue"]["state"], "closed")

    async def test_unknown_dates_remain_unknown_even_for_a_closed_issue(self):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={**ISSUE, "state": "closed"}),
        )) as client:
            result = await GitHubTools(client, TARGET, lambda text, limit: (text, False)).execute("get_issue", "{}")
            self.assertIsNone(result["issue"]["created_at"])
            self.assertIsNone(result["issue"]["closed_at"])


class DraftGroundingAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_checks_leave_two_repair_requests_within_the_existing_limit(self):
        bad_time = label_content()
        bad_time["draft_reply"] = "This will be fixed in version 2.0."
        bad_labels = label_content()
        bad_labels["limitations"] = ["No labels are recommended."]
        result, create, _ = await run_simulated_agent([
            *label_prefix(),
            *[simulated_response([tool_call("get_repo_labels", call_id=f"repeat_labels_{index}")]) for index in range(4)],
            simulated_response(content=bad_time), simulated_response(content=bad_labels),
            simulated_response(content=label_content()),
        ], handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(create.await_count, 8)
        self.assertEqual(result.usage.tool_calls, 7)
        self.assertEqual(len(result.rejected_answers), 2)
        self.assertTrue(all(call.kwargs["tool_choice"] == "none" for call in create.call_args_list[5:]))

    async def test_early_finalization_waits_for_a_successful_search(self):
        result, create, _ = await run_simulated_agent([
            *[simulated_response([tool_call("get_repo_labels", call_id=f"labels_{index}")]) for index in range(5)],
            simulated_response([tool_call("search_issues", '{"query":"export"}', "search")]),
            simulated_response(content=label_content()),
        ], handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(create.call_args_list[5].kwargs["tool_choice"], "auto")
        self.assertEqual(create.call_args_list[6].kwargs["tool_choice"], "none")

    async def test_bad_forecast_uses_existing_bounded_repair_and_keeps_rejected_text(self):
        bad = draft_content("This is expected to be addressed in version 2.0.")
        good = draft_content("In the 2020 discussion, a maintainer expected the timing fix in version 2.0. Current release status has not been verified. The reporter already explained that cleanup is safe to repeat and timing remains the concern.")
        result, create, emit = await run_simulated_agent([
            simulated_response(content=bad), simulated_response(content=good),
        ], handler=draft_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.content.draft_reply, good["draft_reply"])
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(create.call_args_list[0].kwargs["input"], create.call_args_list[1].kwargs["input"])
        self.assertEqual(json.loads(result.rejected_answers[0].output_text)["draft_reply"], bad["draft_reply"])
        self.assertTrue(any("unqualified time/capability" in call.args[1] for call in emit.call_args_list))

    async def test_analysis_date_and_source_timestamps_reach_model_input(self):
        result, create, _ = await run_simulated_agent([
            simulated_response(content=final_content()),
        ], handler=draft_github_response)
        self.assertIn(f"Analysis date (UTC): {result.analysis_date.isoformat()}", create.call_args.kwargs["instructions"])
        issue_output = next(
            item for item in create.call_args_list[0].kwargs["input"]
            if item.get("type") == "function_call_output" and item["call_id"] == "server_get_issue"
        )
        issue_payload = json.loads(issue_output["output"])
        self.assertEqual(issue_payload["issue"]["closed_at"], CLOSED)
        self.assertTrue(any("Created: 2020-01-02" in source.excerpt for source in result.sources))

    async def test_retrospective_reply_with_no_question_is_a_valid_final_answer(self):
        content = draft_content("Thanks for clarifying that cleanup is already safe to repeat. The recorded concern is its timing. The maintainer expected a fix in version 2.0 at the time; the current implementation was not checked.")
        result, create, _ = await run_simulated_agent([
            simulated_response(content=content),
        ], handler=draft_github_response)
        self.assertIsNotNone(result.content)
        self.assertNotIn("?", result.content.draft_reply)
        self.assertEqual(result.content.missing_information, [])
        self.assertEqual(create.await_count, 1)

    async def test_a_still_unanswered_minimal_example_question_is_allowed(self):
        content = draft_content("Thanks for clarifying the timing concern. Could you share a minimal application demonstrating the failure?")
        result, _, _ = await run_simulated_agent([
            simulated_response(content=content),
        ], handler=draft_github_response)
        self.assertIsNotNone(result.content)
        self.assertIn("minimal application", result.content.draft_reply)


class DraftGroundingEvaluationTests(unittest.TestCase):
    def test_common_temporal_check_is_narrow_and_missing_answers_are_not_scored(self):
        for content, expected in (
            (AnalysisContent.model_validate(draft_content("This will be fixed in version 2.0.")), "failed"),
            (AnalysisContent.model_validate(draft_content("A maintainer expected a fix in version 2.0 at the time.")), "passed"),
            (None, "not_scored"),
        ):
            with self.subTest(expected=expected):
                result = AnalysisResult(content=content)
                checks = {item.name: item.status for item in check_result(DATASET.cases[0], result)}
                self.assertEqual(checks["draft_time_claims_attributed"], expected)
                self.assertEqual(functional_checks(result)["draft_time_claims_attributed"], None if content is None else expected == "passed")


if __name__ == "__main__":
    unittest.main()
