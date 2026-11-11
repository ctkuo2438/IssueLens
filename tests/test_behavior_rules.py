"""Offline checks for attribution, bounded corrections and evaluation regression signals."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

import httpx

from backend.app.models import AnalysisContent
from backend.app.quality import reproduction_claim_fields
from evaluation.checks import check_result
from test_evaluation import DATASET, reference_result
from test_mvp import ISSUE, SETTINGS, final_content, run_simulated_agent, simulated_response, tool_call


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "reproduction_claims.json").read_text())


class ReproductionRuleTests(unittest.TestCase):
    def test_known_positive_first_person_forms_require_attribution(self):
        for text in FIXTURE["positive_claims"]:
            with self.subTest(text=text):
                content = final_content()
                content["draft_reply"] = text
                self.assertEqual(reproduction_claim_fields(AnalysisContent.model_validate(content)), ["draft_reply"])

    def test_third_person_attribution_negation_and_other_read_only_actions_are_allowed(self):
        for text in FIXTURE["allowed_attribution_or_negation"]:
            with self.subTest(text=text):
                content = final_content()
                content["draft_reply"] = text
                self.assertEqual(reproduction_claim_fields(AnalysisContent.model_validate(content)), [])

    def test_all_model_written_prose_fields_are_checked(self):
        base = final_content()
        base["comparisons"] = [{
            "issue_number": 7, "relationship": "insufficient_evidence", "reason": "A simulated reason.",
            "similarities": ["A simulated similarity."], "differences": ["A simulated distinction."],
            "source_ids": ["S1", "S4"],
        }]
        base["label_recommendations"] = [{"name": "bug", "reason": "A simulated label reason.", "source_ids": ["S1", "S2"]}]
        for path in (
            ("summary", "text"), ("draft_reply",), ("known_facts", 0, "text"),
            ("missing_information", 0), ("limitations", 0), ("comparisons", 0, "reason"),
            ("comparisons", 0, "similarities", 0), ("comparisons", 0, "differences", 0),
            ("label_recommendations", 0, "reason"),
        ):
            with self.subTest(path=path):
                content = copy.deepcopy(base)
                parent = content
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = "We have reproduced the issue."
                self.assertEqual(len(reproduction_claim_fields(AnalysisContent.model_validate(content))), 1)

    def test_evaluation_flags_the_known_claim_and_does_not_score_missing_final_content(self):
        result = reference_result(4)
        result.content.draft_reply = FIXTURE["positive_claims"][1]
        checks = {item.name: item.status for item in check_result(DATASET.cases[4], result)}
        self.assertEqual(checks["no_first_person_reproduction_claim"], "failed")
        result.content = None
        checks = {item.name: item.status for item in check_result(DATASET.cases[4], result)}
        self.assertEqual(checks["no_first_person_reproduction_claim"], "not_scored")


class BoundedCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_draft_is_corrected_before_acceptance(self):
        invalid = final_content()
        invalid["draft_reply"] = FIXTURE["positive_claims"][0]
        corrected = final_content()
        corrected["draft_reply"] = "The reporter describes a reproducible export failure. Please share any additional error details."
        result, create, emit = await run_simulated_agent([
            simulated_response([tool_call("get_issue")]),
            simulated_response(content=invalid), simulated_response(content=corrected),
        ])
        self.assertEqual(create.await_count, 3)
        self.assertEqual(result.content.draft_reply, corrected["draft_reply"])
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertTrue(any("Unsupported first-person reproduction claim in draft_reply" in call.args[1] for call in emit.call_args_list))
        self.assertIn("third person", create.call_args.kwargs["instructions"])

    async def test_exhausted_budget_rejects_the_unsupported_answer(self):
        invalid = final_content()
        invalid["draft_reply"] = "We reproduced the error locally."
        result, create, emit = await run_simulated_agent([
            simulated_response([tool_call("get_issue")]), simulated_response(content=invalid),
        ], settings=replace(SETTINGS, max_iterations=2))
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertTrue(result.sources)
        self.assertTrue(any("No correction budget remains" in call.args[1] for call in emit.call_args_list))

    async def test_reporter_quote_in_retrieved_source_is_preserved(self):
        quoted = "I can reproduce the export failure on Windows."
        response = {**ISSUE, "body": quoted}
        answer = final_content()
        answer["draft_reply"] = "The reporter can reproduce the export failure on Windows."
        result, _, _ = await run_simulated_agent([
            simulated_response([tool_call("get_issue")]), simulated_response(content=answer),
        ], handler=lambda request: httpx.Response(200, json=response))
        self.assertIsNotNone(result.content)
        self.assertEqual(result.sources[0].excerpt, quoted)
        self.assertEqual(result.content.draft_reply, answer["draft_reply"])


if __name__ == "__main__":
    unittest.main()
