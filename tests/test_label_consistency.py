"""Offline cross-field label checks using simulated model and GitHub responses."""

import copy
from dataclasses import replace
import unittest

from backend.app.models import AnalysisContent, AnalysisResult
from backend.app.quality import label_consistency_fields
from evaluation.checks import check_result
from scripts.acceptance import functional_checks
from test_evaluation import DATASET
from test_labels import label_content, label_github_response, label_prefix
from test_mvp import SETTINGS, final_content, run_simulated_agent, simulated_response


def decision(text, *, labels=True):
    content = label_content() if labels else final_content()
    content["limitations"] = [text]
    return content


class LabelConsistencyRuleTests(unittest.TestCase):
    def check(self, text, *, labels=True):
        return label_consistency_fields(AnalysisContent.model_validate(decision(text, labels=labels)))

    def test_known_zero_claims_conflict_with_nonempty_recommendations(self):
        for text in (
            "No label is recommended because the evidence is insufficient.",
            "The catalog was available, but no additional label recommendation is justified from the evidence alone.",
            "No additional label was obviously justified beyond the evidence here.",
            "No label recommendations were made.",
            "No suitable new labels are recommended.",
            "No recommended labels.",
            "There are no label recommendations.",
            "Label recommendations: none.",
            "None of the labels are recommended.",
            "No labels to recommend.",
            "I do not recommend any additional labels.",
            "We cannot suggest labels.",
            "I am not recommending any labels.",
            "I haven't recommended any labels.",
            "I recommend no labels.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), ["limitations[0]"])

    def test_other_model_prose_cannot_hide_the_same_conflict(self):
        content = AnalysisContent.model_validate(label_content())
        content.summary.text = "No label recommendation is made."
        content.known_facts[0].text = "No label recommendations."
        content.draft_reply = "I do not recommend any labels."
        content.label_recommendations[0].reason = "No label recommendations are being made."
        self.assertEqual(label_consistency_fields(content), ["summary", "draft_reply", "known_facts[0]", "label_recommendations[0].reason"])

    def test_empty_list_can_truthfully_have_no_recommendations(self):
        for text in ("No additional label recommendation is justified.", "No label recommendation is made."):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, labels=False), [])

    def test_passive_made_variants_conflict_with_nonempty_recommendations(self):
        for text in (
            "No label recommendation is made.",
            "No label recommendations are made.",
            "No additional label recommendation has been made.",
            "No new label recommendations have been made.",
            "No further label recommendation is being made.",
            "No label recommendations are currently being made.",
            "No label recommendation is now made.",
            "No label recommendation was being made.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), ["limitations[0]"])

    def test_saved_passive_counterexample_identifies_the_second_limitation(self):
        data = final_content()
        data["label_recommendations"] = [{
            "name": "standards", "reason": "The proposal concerns API standards.", "source_ids": ["S1", "S2"],
        }]
        data["limitations"] = [
            "Implementation and release status were not verified.",
            "I checked the current repository label catalog, but only a small set of active labels is available "
            "and none was clearly justified beyond the feature request itself, so no label recommendation is made.",
        ]
        content = AnalysisContent.model_validate(data)
        before = content.model_dump_json()
        self.assertEqual(label_consistency_fields(content), ["limitations[1]"])
        self.assertEqual(content.model_dump_json(), before)

    def test_passive_claim_exceptions_do_not_confuse_recommendations_with_application(self):
        for text in (
            "No labels were made or added to GitHub.",
            "No labels have been applied; the recommendations remain for review.",
            "No label recommendations were posted to GitHub.",
            "If no label recommendation is made, the recommendation list should be empty.",
            "I am not saying that no label recommendation is made.",
            "It is not true that no label recommendation is made.",
            "The maintainer said no label recommendation was made.",
            "No label recommendation is made by the reporter.",
            "No security label recommendation is made; the listed suggestions remain applicable.",
            "No other label recommendation is made beyond `type:bug` and `needs-reproduction`.",
            "No further label recommendation is made beyond `type:bug` and `needs-reproduction`.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), [])

    def test_application_state_and_qualified_limits_are_not_zero_recommendation_claims(self):
        for text in (
            "No labels were applied to GitHub; suggestions are for review only.",
            "No labels were made or added to GitHub.",
            "The target currently has no labels.",
            "No security label is recommended; type:bug and needs-reproduction remain suggestions.",
            "Recommendations are limited to the loaded catalog; unseen labels were not evaluated.",
            "No additional labels from unread pages are recommended.",
            "The catalog is partial, so the two suggested labels still need review.",
            "The evidence supports recommending type:bug and needs-reproduction; no other active issue label is strongly justified.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), [])

    def test_named_exceptions_must_cover_all_recommendations(self):
        self.assertEqual(self.check("No further labels are recommended beyond `type:bug` and `needs-reproduction`."), [])
        self.assertEqual(self.check("Beyond `type:bug` and `needs-reproduction`, no other labels are recommended."), [])
        self.assertEqual(self.check("No further labels are recommended beyond `type:bug`."), ["limitations[0]"])
        self.assertEqual(self.check("Beyond the available evidence, no labels are recommended, including type:bug and needs-reproduction."), ["limitations[0]"])
        self.assertEqual(self.check("No further label recommendation is made beyond `type:bug`."), ["limitations[0]"])
        self.assertEqual(self.check("Beyond `type:bug` and `needs-reproduction`, no further label recommendation is made."), [])

    def test_known_conditions_negations_and_historical_attribution_are_not_current_decisions(self):
        for text in (
            "If no labels are recommended, the list should be empty.",
            "I am not saying that no labels are recommended.",
            "It is not true that no labels are recommended.",
            "The maintainer said no labels were recommended.",
            "No labels were recommended by the maintainer.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), [])

    def test_check_does_not_rewrite_model_content(self):
        content = AnalysisContent.model_validate(decision("No labels are recommended."))
        before = content.model_dump_json()
        self.assertEqual(label_consistency_fields(content), ["limitations[0]"])
        self.assertEqual(content.model_dump_json(), before)


class LabelConsistencyAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_conflict_repairs_from_unchanged_evidence_and_keeps_supported_labels(self):
        bad = decision("No label recommendation is made.")
        bad["limitations"].insert(0, "Only the loaded catalog was checked.")
        before = copy.deepcopy(bad)
        corrected = decision("The two recommendations need maintainer review; no labels were applied to GitHub.")
        result, create, emit = await run_simulated_agent([
            *label_prefix(), simulated_response(content=bad), simulated_response(content=corrected),
        ], settings=replace(SETTINGS, model="gpt-5.6", reasoning_effort="medium"), handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual([item.name for item in result.content.label_recommendations], ["type:bug", "needs-reproduction"])
        self.assertEqual(create.await_count, 3)
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertEqual(create.call_args_list[-2].kwargs["input"], create.call_args_list[-1].kwargs["input"])
        self.assertEqual(create.call_args_list[-1].kwargs["tool_choice"], "none")
        self.assertEqual(result.context_builds[-1].phase, "repair")
        self.assertEqual(result.context_builds[-1].future_reserve_tokens, 0)
        self.assertEqual(len(result.rejected_answers), 1)
        self.assertIn("limitations[1]", result.rejected_answers[0].reason)
        self.assertIn("no label recommendation is made", result.rejected_answers[0].output_text.lower())
        self.assertTrue(any("Label-decision text conflicts" in call.args[1] for call in emit.call_args_list))
        self.assertTrue(all(call.kwargs["max_output_tokens"] == 2400 for call in create.call_args_list))
        self.assertEqual(bad, before)

    async def test_correction_updates_prose_and_keeps_supported_labels(self):
        bad = decision("No labels are recommended.")
        original = copy.deepcopy(bad)
        result, create, emit = await run_simulated_agent([
            *label_prefix(), simulated_response(content=bad),
            simulated_response(content=decision("Both recommendations need maintainer review; no GitHub labels were applied.")),
        ], handler=label_github_response)
        self.assertEqual([item.name for item in result.content.label_recommendations], ["type:bug", "needs-reproduction"])
        self.assertEqual(create.await_count, 3)
        warning = next(call.args[1] for call in emit.call_args_list if call.args[0] == "warning")
        self.assertIn("Label-decision text conflicts", warning)
        self.assertIn("limitations[0]", warning)
        self.assertIn("Requesting a correction", warning)
        self.assertIn("Current recommended labels: ['type:bug', 'needs-reproduction']", create.call_args.kwargs["instructions"])
        self.assertEqual(bad, original)

    async def test_reassessed_empty_list_is_a_valid_correction(self):
        result, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=decision("No labels are recommended.")),
            simulated_response(content=decision("No additional labels are justified.", labels=False)),
        ], handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.content.label_recommendations, [])

    async def test_budget_exhaustion_rejects_answer_without_silently_stripping_prose_or_labels(self):
        result, create, emit = await run_simulated_agent([
            *label_prefix(), simulated_response(content=decision("No label recommendation is made.")),
        ], settings=replace(SETTINGS, max_iterations=2), handler=label_github_response)
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(result.usage.tool_calls, 3)
        warnings = [call.args[1] for call in emit.call_args_list if call.args[0] == "warning"]
        self.assertTrue(any("Label-decision text conflicts" in warning and "No correction budget remains" in warning for warning in warnings))
        self.assertTrue(result.sources)
        self.assertTrue(result.repo_labels)

    async def test_repeated_conflicts_stop_at_the_existing_iteration_limit(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=decision("No label recommendation is made.")),
            simulated_response(content=decision("No label recommendations are being made.")),
        ], settings=replace(SETTINGS, max_iterations=3), handler=label_github_response)
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 3)

    async def test_normal_coverage_limitations_need_no_correction(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=decision("Only the loaded catalog was checked; suggestions remain for review.")),
        ], handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(create.await_count, 2)


class LabelConsistencyEvaluationTests(unittest.TestCase):
    def test_common_check_flags_conflicts_and_does_not_count_missing_answers_as_consistent(self):
        for content, expected in (
            (AnalysisContent.model_validate(decision("No labels are recommended.")), "failed"),
            (AnalysisContent.model_validate(decision("No label recommendation is made.")), "failed"),
            (AnalysisContent.model_validate(label_content()), "passed"),
            (AnalysisContent.model_validate(final_content()), "passed"),
            (None, "not_scored"),
        ):
            with self.subTest(expected=expected):
                result = AnalysisResult(content=content)
                before = result.model_dump_json()
                checks = {check.name: check.status for check in check_result(DATASET.cases[0], result)}
                self.assertEqual(checks["label_decision_consistent"], expected)
                self.assertEqual(functional_checks(result)["label_decision_consistent"], None if expected == "not_scored" else expected == "passed")
                self.assertEqual(result.model_dump_json(), before)


if __name__ == "__main__":
    unittest.main()
