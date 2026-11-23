"""Offline checks for maintainer-ready drafts; no model or GitHub network calls."""

from dataclasses import replace
import unittest

from backend.app.quality import draft_has_service_offer
from evaluation.checks import check_result
from test_evaluation import DATASET, reference_result
from test_mvp import SETTINGS, final_content, run_simulated_agent, simulated_response


ORIGINAL_OFFER = "If you want, I can help turn this into a more concrete migration plan or checklist for 3.2/4.0."


class DraftVoiceTests(unittest.TestCase):
    def test_observed_offer_and_common_service_closings_are_detected(self):
        for text in (
            ORIGINAL_OFFER,
            "Thanks for the details. If you'd like, I can help draft a plan.",
            "If you would like, we could help prepare a checklist.",
            "I can help you write a migration guide.",
            "Would you like me to create a checklist?",
            "Let me know if you’d like me to draft a response.",
        ):
            with self.subTest(text=text):
                self.assertTrue(draft_has_service_offer(text))

    def test_normal_maintainer_questions_and_next_steps_are_allowed(self):
        for text in (
            "Could you provide a minimal reproduction?",
            "A useful next step would be to outline the intended API changes and migration examples.",
            "If you want to submit a patch, please include compatibility tests.",
            "I can review a pull request once compatibility cases are covered.",
            "Please document which changes need deprecation warnings.",
        ):
            with self.subTest(text=text):
                self.assertFalse(draft_has_service_offer(text))

    def test_evaluation_checks_only_draft_voice_and_leaves_missing_content_unscored(self):
        result = reference_result()
        result.content.summary.text = f"The reporter wrote: {ORIGINAL_OFFER}"
        checks = {item.name: item.status for item in check_result(DATASET.cases[0], result)}
        self.assertEqual(checks["maintainer_draft_voice"], "passed")
        result.content.draft_reply += " " + ORIGINAL_OFFER
        checks = {item.name: item.status for item in check_result(DATASET.cases[0], result)}
        self.assertEqual(checks["maintainer_draft_voice"], "failed")
        result.content = None
        checks = {item.name: item.status for item in check_result(DATASET.cases[0], result)}
        self.assertEqual(checks["maintainer_draft_voice"], "not_scored")


class DraftCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_offer_requires_correction_before_acceptance(self):
        invalid = final_content()
        invalid["draft_reply"] += " " + ORIGINAL_OFFER
        corrected = final_content()
        corrected["draft_reply"] = "Thanks for reporting this. Please provide a minimal reproduction and the exact export error."
        result, create, emit = await run_simulated_agent([
            simulated_response(content=invalid), simulated_response(content=corrected),
        ])
        self.assertEqual(create.await_count, 2)
        self.assertEqual(result.content.draft_reply, corrected["draft_reply"])
        self.assertIn("maintainer comment", create.call_args.kwargs["instructions"])
        self.assertTrue(any("assistant-style service offer" in call.args[1] for call in emit.call_args_list))

    async def test_exhausted_budget_keeps_sources_without_accepting_the_offer(self):
        invalid = final_content()
        invalid["draft_reply"] = ORIGINAL_OFFER
        result, create, emit = await run_simulated_agent([
            simulated_response(content=invalid),
        ], settings=replace(SETTINGS, max_iterations=1))
        self.assertEqual(create.await_count, 1)
        self.assertIsNone(result.content)
        self.assertTrue(result.sources)
        self.assertTrue(any("No correction budget remains" in call.args[1] for call in emit.call_args_list))


if __name__ == "__main__":
    unittest.main()
