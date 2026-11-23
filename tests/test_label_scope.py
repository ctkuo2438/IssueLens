"""Offline checks for issue-incompatible label scopes with simulated providers."""

import copy
from dataclasses import replace
import json
import unittest

import httpx

from backend.app.models import AnalysisContent, AnalysisResult, RepositoryLabel
from backend.app.quality import is_pr_only_label, pr_only_recommendations
from evaluation.checks import check_result
from evaluation.snapshots import load_dataset
from scripts.acceptance import functional_checks
from test_evaluation import DATASET_PATH
from test_labels import FIXTURE, label_github_response, label_prefix
from test_mvp import SETTINGS, final_content, run_simulated_agent, simulated_response


SCOPED_LABELS = [
    {"name": "python", "description": "Pull requests that update Python code", "color": "123456"},
    {"name": "shared", "description": "For issues and pull requests about packaging", "color": "123456"},
    {"name": "linked-pr", "description": "Bug reports with a linked pull request", "color": "123456"},
]


def scoped_github_response(request):
    if request.url.path.endswith("/labels"):
        return httpx.Response(200, json=[*FIXTURE["labels"], *SCOPED_LABELS])
    return label_github_response(request)


def recommended(*names):
    content = final_content()
    content["label_recommendations"] = [
        {"name": name, "reason": "The report concerns the catalog topic.", "source_ids": ["S1", "S2"]}
        for name in names
    ]
    return content


class LabelScopeRuleTests(unittest.TestCase):
    def test_known_explicit_pull_request_scopes_are_detected(self):
        for description in (
            "Pull requests that update Python code",
            "Pull requests that update documentation",
            "Pull requests fixing reported issues",
            "For pull requests with dependency updates",
            "For PRs",
            "This label is intended for PRs that add tests",
            "PRs only",
            "PR-only label",
            "Only for pull requests",
            "Use exclusively on PRs",
            "This label is restricted to pull requests",
            "Only pull requests should receive this label",
            "  PULL-REQUESTS\nthat update code  ",
        ):
            with self.subTest(description=description):
                self.assertTrue(is_pr_only_label(description))

    def test_shared_scopes_are_not_mistaken_for_pr_only(self):
        for description in (
            "For issues and pull requests",
            "For pull requests and issues that concern testing",
            "PRs or issues",
            "Issues/PRs",
            "For PRs & issues",
            "For PRs, and issues",
            "For issues, or PRs",
        ):
            with self.subTest(description=description):
                self.assertFalse(is_pr_only_label(description))

    def test_mentions_negations_and_unspecified_scopes_remain_available(self):
        for description in (
            "Bug reports with a linked pull request",
            "Track a bug fixed by PRs",
            "PRs welcome",
            "Not for PRs",
            "Not only for PRs",
            "Not just PR-only",
            "This is not a PR-only label",
            "This label is not limited to PRs",
            "Python runtime problems",
            "",
        ):
            with self.subTest(description=description):
                self.assertFalse(is_pr_only_label(description))

    def test_report_check_uses_catalog_description_and_canonical_name(self):
        content = AnalysisContent.model_validate(recommended("PYTHON", "shared"))
        labels = [RepositoryLabel.model_validate(row) for row in SCOPED_LABELS]
        self.assertEqual(pr_only_recommendations(content, labels), ["python"])
        generic = [RepositoryLabel(name="python", description="Python runtime problems", color="123456")]
        self.assertEqual(pr_only_recommendations(content, generic), [])


class LabelScopeAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_warns_early_without_removing_or_rewriting_catalog_evidence(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=final_content()),
        ], handler=scoped_github_response)
        output = json.loads(create.call_args_list[0].kwargs["input"][-1]["output"])
        self.assertEqual(output["excluded_from_issue_recommendations"], ["python"])
        source = json.loads(output["source"]["excerpt"])
        self.assertEqual(next(row for row in source["labels"] if row["name"] == "python")["description"], SCOPED_LABELS[0]["description"])
        self.assertTrue(source["complete"])
        self.assertTrue(result.labels_complete)
        self.assertIn("python", [label.name for label in result.repo_labels])
        self.assertIn("Do not recommend PR-only labels", create.call_args.kwargs["instructions"])

    async def test_invalid_scope_requests_a_correction_that_preserves_supported_labels(self):
        result, create, emit = await run_simulated_agent([
            *label_prefix(), simulated_response(content=recommended("PYTHON", "TYPE:BUG")),
            simulated_response(content=recommended("TYPE:BUG")),
        ], handler=scoped_github_response)
        self.assertEqual([item.name for item in result.content.label_recommendations], ["type:bug"])
        self.assertEqual(create.await_count, 3)
        warning = next(call.args[1] for call in emit.call_args_list if call.args[0] == "warning")
        self.assertIn("Label 'python' is scoped to pull requests", warning)
        self.assertIn("Requesting a correction", warning)
        self.assertIn("Remove it", create.call_args.kwargs["instructions"])

    async def test_exhausted_budget_rejects_answer_instead_of_silently_deleting_labels(self):
        result, create, emit = await run_simulated_agent([
            *label_prefix(), simulated_response(content=recommended("python", "type:bug")),
        ], settings=replace(SETTINGS, max_iterations=2), handler=scoped_github_response)
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(result.usage.tool_calls, 3)
        warnings = [call.args[1] for call in emit.call_args_list if call.args[0] == "warning"]
        self.assertTrue(any("scoped to pull requests" in warning and "No correction budget remains" in warning for warning in warnings))

    async def test_empty_list_is_a_valid_correction(self):
        result, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=recommended("python")),
            simulated_response(content=final_content()),
        ], handler=scoped_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.content.label_recommendations, [])

    async def test_shared_and_linked_pr_labels_can_still_be_recommended(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=recommended("shared", "linked-pr")),
        ], handler=scoped_github_response)
        self.assertEqual([item.name for item in result.content.label_recommendations], ["shared", "linked-pr"])
        self.assertEqual(create.await_count, 2)

    async def test_python_name_is_not_blocked_when_description_applies_to_issues(self):
        def generic_python(request):
            if request.url.path.endswith("/labels"):
                labels = copy.deepcopy(SCOPED_LABELS)
                labels[0]["description"] = "Python runtime bug reports"
                return httpx.Response(200, json=[*FIXTURE["labels"], *labels])
            return label_github_response(request)

        result, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=recommended("PYTHON")),
        ], handler=generic_python)
        self.assertEqual([item.name for item in result.content.label_recommendations], ["python"])


class LabelScopeEvaluationTests(unittest.TestCase):
    def test_common_checker_flags_existing_outputs_without_rewriting_them(self):
        result = AnalysisResult(
            content=AnalysisContent.model_validate(recommended("python")), labels_source_id="S2",
            repo_labels=[RepositoryLabel.model_validate(row) for row in SCOPED_LABELS],
        )
        case = load_dataset(DATASET_PATH).cases[0]
        before = result.model_dump_json()
        checks = {check.name: check.status for check in check_result(case, result)}
        self.assertEqual(checks["no_known_pr_only_label_recommendations"], "failed")
        self.assertFalse(functional_checks(result)["no_known_pr_only_label_recommendations"])
        self.assertEqual(result.model_dump_json(), before)

    def test_missing_answer_or_catalog_is_not_scored_as_correct_abstention(self):
        case = load_dataset(DATASET_PATH).cases[0]
        for result in (AnalysisResult(), AnalysisResult(content=AnalysisContent.model_validate(final_content()))):
            with self.subTest(result=result):
                checks = {check.name: check.status for check in check_result(case, result)}
                self.assertEqual(checks["no_known_pr_only_label_recommendations"], "not_scored")
                self.assertIsNone(functional_checks(result)["no_known_pr_only_label_recommendations"])


if __name__ == "__main__":
    unittest.main()
