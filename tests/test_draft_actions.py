"""Narrow draft regressions with simulated providers and unchanged historical data."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest

import httpx

from backend.app.models import AnalysisContent, AnalysisResult
from backend.app.quality import draft_action_claims, draft_closure_claims
from evaluation.checks import check_result
from scripts.acceptance import functional_checks
from test_evaluation import DATASET
from test_mvp import COMMENT, ISSUE, SETTINGS, final_content, run_simulated_agent, simulated_response
from test_required_lookups import PolicyServer, content_with_comparison, prefix, run_policy, step


OBSERVED_DRAFT = (
    "Thanks for the feature request. RFC5861 support in `ResponseCacheControl` is a "
    "standards-oriented enhancement request, and I’m closing this as the issue is "
    "already resolved/handled in the tracker. If you still need this behavior in "
    "current Werkzeug, could you point to the specific API shape you’d want exposed "
    "for `stale-while-revalidate` and `stale-if-error`?"
)
SAFE_DRAFT = (
    "The retrieved issue record is closed. That state does not establish whether "
    "a released version implements the request. Please check current implementation "
    "before proposing any remaining changes."
)


def draft_content(text):
    content = final_content()
    content["draft_reply"] = text
    return content


class DraftActionRuleTests(unittest.TestCase):
    def test_observed_false_action_and_tracker_resolution_are_both_detected(self):
        self.assertEqual(draft_action_claims(OBSERVED_DRAFT), ["unperformed_maintainer_action"])
        self.assertEqual(draft_closure_claims(OBSERVED_DRAFT), ["closure_implies_resolution"])

    def test_known_current_and_completed_repository_mutations_are_flagged(self):
        for text in (
            "I'm closing this issue.", "We have closed #42.", "I reopened the report.",
            "We are now reopening this.", "I've labeled the issue.", "I labelled this as a bug.",
            "We assigned this issue to a maintainer.", "I've just applied the `bug` label.",
            "I added the bug label.", "We applied needs-info labels.",
            "I removed the label.", "We merged the pull request.", "I posted a comment.",
            "We have published this reply.",
        ):
            with self.subTest(text=text):
                self.assertTrue(draft_action_claims(text))

    def test_negation_suggestions_questions_and_read_actions_remain_allowed(self):
        for text in (
            "I have not closed this issue.", "I'm not closing this.", "We never assigned it.",
            "We could close this issue after reviewing the evidence.",
            "I would recommend reopening the report if the problem remains.",
            "Please apply the bug label only after review.", "Could I close this issue?",
            "I read the issue and retrieved the comments.", "We have not posted a reply.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_action_claims(text), [])

    def test_non_tracker_objects_do_not_count_as_repository_actions(self):
        for text in (
            "I closed the database connection in the example.",
            "We assigned a value to the variable.", "I merged the dictionaries in this snippet.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_action_claims(text), [])

    def test_third_person_and_explicitly_attributed_quotes_are_allowed(self):
        for text in (
            "A maintainer closed this issue in 2020.",
            'In 2020, a maintainer wrote: "I closed this issue."',
            "The author said, ‘I've applied the label.’",
            '@reviewer wrote: "I merged the pull request."',
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_action_claims(text), [])

    def test_earlier_attribution_does_not_excuse_a_new_own_voice_action(self):
        for text in (
            "A maintainer said the report was useful; I'm closing this issue.",
            'A maintainer wrote: "I closed this issue." I have posted a reply.',
            "The reporter said this reproduces, and we assigned the issue.",
        ):
            with self.subTest(text=text):
                self.assertTrue(draft_action_claims(text))


class DraftClosureRuleTests(unittest.TestCase):
    def test_positive_forward_reverse_and_tracker_inferences_are_flagged(self):
        for text in (
            "The issue is closed, so it is fixed.",
            "Closed status proves the feature is implemented.",
            "The issue was closed; therefore the problem was resolved.",
            "It is fixed because the issue is closed.",
            "The problem is resolved since the report was closed.",
            "The issue is closed and resolved.", "Closed = fixed.",
            "The issue is already resolved/handled in the tracker.",
        ):
            with self.subTest(text=text):
                self.assertTrue(draft_closure_claims(text))

    def test_negated_relations_and_unknown_implementation_remain_allowed(self):
        for text in (
            SAFE_DRAFT, "Closed does not mean fixed.",
            "Closed doesn't necessarily prove that it is resolved.",
            "The issue is not fixed because it is closed.",
            "It is not resolved in the tracker.",
            "The issue is closed, so we cannot assume it is fixed.",
            "The issue is closed but the fix status is unknown.",
            "GitHub records it as closed/completed; implementation remains unchecked.",
            "The issue was closed as a duplicate.",
            "Does closed mean fixed?", "Please verify the implementation before reopening.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_closure_claims(text), [])

    def test_reported_historical_outcomes_do_not_become_current_fix_assertions(self):
        for text in (
            "A maintainer reported that the issue was closed and resolved in 2020.",
            "The author wrote that it was resolved in the tracker at that time.",
            "A maintainer reported a tested fix, then closed the issue.",
            "According to the retrieved comment, the maintainer closed the issue after reviewing a fix.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_closure_claims(text), [])

    def test_uncertain_and_conditional_relations_are_not_positive_fix_claims(self):
        for text in (
            "I cannot confirm that a closed issue means it was fixed.",
            "It is unclear whether closed means resolved.",
            "Do not assume closed means fixed.",
            "I do not claim this is closed and resolved.",
            "If the issue is closed and resolved, please link the evidence.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_closure_claims(text), [])

    def test_uncertainty_does_not_apply_to_an_independent_positive_claim(self):
        text = "I cannot confirm a release. The issue is closed and resolved."
        self.assertTrue(draft_closure_claims(text))

    def test_closed_state_does_not_swallow_separate_attributed_fix_evidence(self):
        for text in (
            "The issue is closed, but a maintainer confirms that the bug was fixed.",
            "The issue was closed after a maintainer reported a verified fix.",
        ):
            with self.subTest(text=text):
                self.assertEqual(draft_closure_claims(text), [])

    def test_negation_and_attribution_cannot_hide_an_independent_inference(self):
        for text in (
            "I did not test it. The issue is closed, so it is fixed.",
            "A maintainer reported a failure; the issue is closed and resolved.",
            "The tracker says it is closed and resolved.",
        ):
            with self.subTest(text=text):
                self.assertTrue(draft_closure_claims(text))

    def test_checks_do_not_mutate_text_or_retained_sources(self):
        content = draft_content(OBSERVED_DRAFT)
        original = deepcopy(content)
        draft_action_claims(content["draft_reply"])
        draft_closure_claims(content["draft_reply"])
        self.assertEqual(content, original)


class DraftActionAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_problems_share_one_bounded_repair_from_the_same_evidence(self):
        result, create, emit = await run_simulated_agent([
            simulated_response(content=draft_content(OBSERVED_DRAFT)),
            simulated_response(content=draft_content(SAFE_DRAFT)),
        ])
        self.assertEqual(create.await_count, 2)
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual(result.content.draft_reply, SAFE_DRAFT)
        self.assertEqual(create.call_args_list[0].kwargs["input"], create.call_args_list[1].kwargs["input"])
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertNotIn("RFC5861 support in", json.dumps(create.call_args.kwargs))
        feedback = create.call_args.kwargs["instructions"]
        self.assertIn("unperformed_maintainer_action", feedback)
        self.assertIn("closure_implies_resolution", feedback)
        rejected = result.rejected_answers[0]
        self.assertEqual(json.loads(rejected.output_text)["draft_reply"], OBSERVED_DRAFT)
        self.assertTrue(any("unsupported action/status" in call.args[1] for call in emit.call_args_list))

    async def test_each_error_independently_blocks_final_acceptance(self):
        for text, problem in (
            ("I closed this issue.", "unperformed_maintainer_action"),
            ("Closed means fixed.", "closure_implies_resolution"),
        ):
            with self.subTest(problem=problem):
                result, _, _ = await run_simulated_agent([
                    simulated_response(content=draft_content(text)),
                    simulated_response(content=draft_content(SAFE_DRAFT)),
                ])
                self.assertEqual(result.content.draft_reply, SAFE_DRAFT)
                self.assertIn(problem, result.rejected_answers[0].reason)

    async def test_exhausted_budget_keeps_partial_sources_without_a_bad_final(self):
        result, create, emit = await run_simulated_agent([
            simulated_response(content=draft_content(OBSERVED_DRAFT)),
        ], settings=replace(SETTINGS, max_iterations=1))
        self.assertEqual(create.await_count, 1)
        self.assertIsNone(result.content)
        self.assertTrue(result.sources)
        self.assertEqual(len(result.rejected_answers), 1)
        self.assertTrue(any("No correction budget remains" in call.args[1] for call in emit.call_args_list))

    async def test_complete_lookup_policy_and_new_repair_work_together(self):
        bad = content_with_comparison()
        bad["draft_reply"] = OBSERVED_DRAFT
        good = content_with_comparison()
        good["draft_reply"] = SAFE_DRAFT
        server = PolicyServer()
        result, create, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41),
            simulated_response(content=bad), simulated_response(content=good),
        ], server)
        self.assertEqual(create.await_count, 4)
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(result.similar_issue_check.compared_candidate_numbers, [41])
        self.assertTrue(functional_checks(result)["minimum_similar_issue_checks_completed"])
        self.assertEqual(create.call_args_list[2].kwargs["input"], create.call_args_list[3].kwargs["input"])
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")

    async def test_existing_reported_source_action_is_not_rewritten_or_executed(self):
        source_body = 'In 2020, a maintainer wrote: "I closed this issue."'

        def respond(request):
            if request.url.path.endswith("/labels"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{**COMMENT, "body": source_body}] if request.url.path.endswith("/comments")
                                  else {**ISSUE, "comments": 1})

        content = draft_content(source_body)
        result, create, _ = await run_simulated_agent([simulated_response(content=content)], handler=respond)
        self.assertEqual(result.content.draft_reply, source_body)
        self.assertIn(source_body, result.sources[1].excerpt)
        self.assertEqual(create.await_count, 1)
        self.assertEqual(result.rejected_answers, [])


class DraftActionEvaluationTests(unittest.TestCase):
    def test_common_graders_agree_and_missing_content_is_unscored(self):
        for text, expected in ((OBSERVED_DRAFT, "failed"), (SAFE_DRAFT, "passed"), (None, "not_scored")):
            result = AnalysisResult(content=AnalysisContent.model_validate(draft_content(text)) if text else None)
            checks = {item.name: item.status for item in check_result(DATASET.cases[0], result)}
            live = functional_checks(result)
            for name in ("no_known_unperformed_maintainer_action", "no_known_closure_resolution_inference"):
                self.assertEqual(checks[name], expected)
                self.assertEqual(live[name], None if text is None else expected == "passed")

    def test_new_checks_apply_to_draft_only_not_attributed_summary_text(self):
        content = draft_content(SAFE_DRAFT)
        content["summary"]["text"] = f"The source text says: {OBSERVED_DRAFT}"
        result = AnalysisResult(content=AnalysisContent.model_validate(content))
        checks = functional_checks(result)
        self.assertTrue(checks["no_known_unperformed_maintainer_action"])
        self.assertTrue(checks["no_known_closure_resolution_inference"])


if __name__ == "__main__":
    unittest.main()
