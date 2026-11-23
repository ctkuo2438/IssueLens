"""Offline evaluation checks. Both GitHub evidence and model responses are simulated."""

from contextlib import redirect_stdout
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import AuthenticationError
from pydantic import ValidationError

from backend.app.agent import AgentRun
from backend.app.models import AnalysisContent, AnalysisResult, IssueCandidate, IssueInfo, IssueSearch, Source
from evaluation.checks import check_result, summarize_cases
from evaluation.snapshots import Dataset, SnapshotGitHub, load_dataset
from scripts import evaluate
from test_mvp import OfflineEncoding, SETTINGS, final_content, simulated_response, tool_call


DATASET_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "cases.json"
DATASET = load_dataset(DATASET_PATH)


def reference_result(case_index=0):
    """Build an explicitly simulated accepted result for checking the checker."""
    case = DATASET.cases[case_index]
    target = case.snapshot.target
    issue = next(item for item in case.snapshot.issues if item.number == target.number)
    content = final_content()
    content["summary"]["text"] = issue.title
    content["known_facts"] = []
    content["comparisons"] = [{
        "issue_number": item.issue_number, "relationship": item.relationship,
        "reason": "This is a simulated reference comparison, not model analysis.",
        "similarities": ["A simulated shared condition."], "differences": ["A simulated distinction."],
        "source_ids": ["S1", "S4"],
    } for item in case.expected.comparisons]
    content["label_recommendations"] = [{
        "name": name, "reason": "This is a simulated reference suggestion, not model analysis.",
        "source_ids": ["S1", "S2"],
    } for name in case.expected.required_labels]
    sources = [
        Source(id="S1", kind="issue", title=issue.title, url=target.url, excerpt=issue.body),
        Source(id="S2", kind="repo_labels", title="Fictional catalog", url=f"{target.url}/labels", excerpt="Fictional catalog excerpt."),
        Source(id="S3", kind="search", title="Fictional search", url=f"{target.url}/search", excerpt="Fictional search excerpt."),
    ]
    candidates = []
    for item in case.expected.comparisons:
        candidates.append(IssueCandidate(number=item.issue_number, title="Fictional candidate", url=f"https://github.com/{target.owner}/{target.repo}/issues/{item.issue_number}", state="open", source_id="S4"))
        sources.append(Source(id="S4", kind="related_issue", title="Fictional candidate", url=candidates[-1].url, excerpt="Fictional candidate excerpt."))
    return AnalysisResult(
        issue=IssueInfo(title=issue.title, url=target.url, number=target.number, state=issue.state, author="fixture-reporter", labels=issue.labels, comment_count=len(case.snapshot.comments)),
        content=AnalysisContent.model_validate(content), sources=sources, candidates=candidates,
        searches=[IssueSearch(query="A simulated successful query", total_count=len(candidates), incomplete_results=False, candidate_numbers=[item.number for item in candidates], source_id="S3")],
        repo_labels=DATASET.labels, labels_source_id="S2", labels_complete=True,
    )


class DatasetTests(unittest.TestCase):
    def test_five_distinct_cases_have_reviewable_references(self):
        self.assertEqual(len(DATASET.cases), 5)
        self.assertEqual({case.expected.comparisons[0].relationship for case in DATASET.cases[:3]}, {"possible_duplicate", "related_but_different", "insufficient_evidence"})
        self.assertTrue(DATASET.cases[2].expected.missing_information_required)
        self.assertEqual(DATASET.cases[4].expected.allowed_labels, [])
        self.assertEqual(DATASET.annotation_status, "assistant_authored_reference_pending_human_review")

    def test_invalid_reference_data_is_rejected_before_model_calls(self):
        original = json.loads(DATASET_PATH.read_text())
        for problem in ("duplicate_id", "missing_candidate", "target_comparison", "unknown_label", "existing_label", "contradictory_empty", "missing_target"):
            with self.subTest(problem=problem):
                data = copy.deepcopy(original)
                first = data["cases"][0]
                if problem == "duplicate_id":
                    first["id"] = data["cases"][1]["id"]
                elif problem == "missing_candidate":
                    first["expected"]["comparisons"][0]["issue_number"] = 999
                elif problem == "target_comparison":
                    first["expected"]["comparisons"][0]["issue_number"] = 101
                elif problem == "unknown_label":
                    first["expected"]["allowed_labels"].append("invented")
                elif problem == "existing_label":
                    first["snapshot"]["issues"][0]["labels"] = ["bug"]
                elif problem == "contradictory_empty":
                    first["expected"]["no_candidates_expected"] = True
                else:
                    first["snapshot"]["issues"][0]["number"] = 999
                with self.assertRaises(ValidationError):
                    Dataset.model_validate(data)


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_tools_use_scoped_fixed_evidence_with_no_github_network(self):
        from backend.app.github import GitHubTools

        snapshot = SnapshotGitHub(DATASET.cases[0].snapshot, DATASET.labels)
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(snapshot.respond)) as client:
            tools = GitHubTools(client, snapshot.snapshot.target, lambda text, limit: (text, False))
            await tools.execute("get_issue", "{}")
            search = await tools.execute("search_issues", '{"query":"CSV Windows"}')
            self.assertEqual(search["search"]["candidate_numbers"], [91])
            await tools.execute("get_related_issue", '{"issue_number":91}')
            await tools.execute("get_repo_labels", "{}")
            await tools.execute("get_repo_readme", "{}")
            comments = await tools.execute("get_issue_comments", '{"page":1}')
        self.assertEqual(comments["comments"], [])
        self.assertEqual(len(snapshot.requests), 6)
        self.assertTrue(all(item["method"] == "GET" for item in snapshot.requests))
        self.assertTrue(all("headers" not in item for item in snapshot.requests))

    async def test_keyword_matcher_does_not_return_expected_candidates_unconditionally(self):
        snapshot = SnapshotGitHub(DATASET.cases[0].snapshot, DATASET.labels)
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(snapshot.respond)) as client:
            for query in ('repo:example/eval-export is:issue in:title,body "unmatchedword"', 'repo:example/eval-export is:issue in:title,body "CSV" "unmatchedword"'):
                response = await client.get("/search/issues", params={"q": query})
                self.assertEqual(response.json()["items"], [])
            response = await client.get("/repos/other/project/issues/101")
            self.assertEqual(response.status_code, 404)
            response = await client.post("/repos/example/eval-export/labels")
            self.assertEqual(response.status_code, 403)

    async def test_reference_notes_and_review_questions_never_enter_http_payloads(self):
        case = DATASET.cases[0].model_copy(deep=True)
        case.reference_notes = "REFERENCE_CANARY_HIDDEN_FROM_MODEL"
        case.review_questions = ["REVIEW_CANARY_HIDDEN_FROM_MODEL"]
        snapshot = SnapshotGitHub(case.snapshot, DATASET.labels)
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(snapshot.respond)) as client:
            payloads = []
            for path in ("/repos/example/eval-export/issues/101", "/repos/example/eval-export/labels", "/repos/example/eval-export/readme"):
                payloads.append((await client.get(path)).text)
        self.assertNotIn("CANARY", " ".join(payloads))


class GradingTests(unittest.TestCase):
    def statuses(self, index, result):
        return {check.name: check.status for check in check_result(DATASET.cases[index], result)}

    def test_reference_outputs_meet_automatic_checks_but_leave_human_review_pending(self):
        cases = [check_result(case, reference_result(index)) for index, case in enumerate(DATASET.cases)]
        summary = summarize_cases(cases)
        self.assertEqual(summary["cases_meeting_automated_expectations"], 5)
        self.assertEqual(summary["human_review_status"], "pending")

    def test_candidate_discovery_reading_and_classification_fail_separately(self):
        result = reference_result()
        result.candidates = []
        status = self.statuses(0, result)
        self.assertEqual(status["candidate_91_found"], "failed")
        self.assertEqual(status["candidate_91_read"], "failed")
        result = reference_result()
        result.candidates[0].source_id = None
        status = self.statuses(0, result)
        self.assertEqual(status["candidate_91_found"], "passed")
        self.assertEqual(status["candidate_91_read"], "failed")
        result = reference_result()
        result.content.comparisons[0].relationship = "related_but_different"
        status = self.statuses(0, result)
        self.assertEqual(status["candidate_91_read"], "passed")
        self.assertEqual(status["candidate_91_relationship"], "failed")

    def test_missing_final_answer_is_not_successful_label_abstention(self):
        result = reference_result(4)
        self.assertEqual(self.statuses(4, result)["labels_within_reference"], "passed")
        result.content = None
        status = self.statuses(4, result)
        self.assertEqual(status["final_answer_accepted"], "failed")
        self.assertEqual(status["labels_within_reference"], "not_scored")
        self.assertEqual(status["no_comparisons"], "not_scored")

    def test_skipped_search_and_catalog_are_not_successful_empty_checks(self):
        result = reference_result(4)
        result.searches = []
        result.labels_source_id = None
        status = self.statuses(4, result)
        self.assertEqual(status["similar_search_completed"], "failed")
        self.assertEqual(status["no_eligible_candidates"], "not_scored")
        self.assertEqual(status["label_catalog_loaded"], "failed")

    def test_wrong_labels_missing_questions_and_invented_citations_are_detected(self):
        result = reference_result(2)
        result.content.label_recommendations[0].name = "security"
        result.content.missing_information = []
        result.content.summary.source_ids = ["S999"]
        status = self.statuses(2, result)
        self.assertEqual(status["required_labels_present"], "failed")
        self.assertEqual(status["labels_within_reference"], "failed")
        self.assertEqual(status["missing_questions_present"], "failed")
        self.assertEqual(status["retrieved_citation_ids"], "failed")

    def test_empty_summary_is_not_a_five_case_success(self):
        summary = summarize_cases([])
        self.assertEqual(summary["cases_attempted"], 0)
        self.assertEqual(summary["cases_meeting_automated_expectations"], 0)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_case_runner_saves_provenance_outcomes_and_usage_with_simulated_sdk(self):
        responses = []
        for index, case in enumerate(DATASET.cases):
            # The server reads the issue, any comments and the labels before the model's first request.
            responses.append(simulated_response([tool_call("search_issues", json.dumps({"query": {0: "CSV Windows", 1: "FreeHost deployment", 2: "export fails", 3: "keyboard shortcut", 4: "PNG encoder"}[index]}), "call_search")]))
            for comparison in case.expected.comparisons:
                responses.append(simulated_response([tool_call("get_related_issue", json.dumps({"issue_number": comparison.issue_number}), "call_candidate")]))
            if case.expected.no_candidates_expected:
                responses.append(simulated_response([tool_call("search_issues", json.dumps({"query": {3: "keyboard", 4: "PNG"}[index]}), "call_broader_search")]))
            content = reference_result(index).content.model_dump()
            if case.snapshot.comments:
                for suggestion in content["label_recommendations"]:
                    suggestion["source_ids"] = ["S3" if source_id == "S2" else source_id for source_id in suggestion["source_ids"]]
                for comparison in content["comparisons"]:
                    comparison["source_ids"] = ["S5" if source_id == "S4" else source_id for source_id in comparison["source_ids"]]
            responses.append(simulated_response(content=content))
        create = AsyncMock(side_effect=responses)
        client = AsyncMock()
        client.__aenter__.return_value.responses.create = create
        settings = replace(SETTINGS, github_token="OFFLINE_GITHUB_CREDENTIAL_CANARY", reasoning_effort="medium")
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(evaluate, "OUTPUT_ROOT", Path(temporary)),
            patch.object(evaluate.Settings, "from_env", return_value=settings),
            patch("backend.app.agent.AsyncOpenAI", return_value=client),
            patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
            redirect_stdout(io.StringIO()),
        ):
            path = await evaluate.main()
            raw_report = path.read_text()
            report = json.loads(raw_report)
            self.assertEqual((path.parent / "cases.json").read_bytes(), DATASET_PATH.read_bytes())
        self.assertEqual(report["cases_expected"], 5)
        self.assertEqual(report["reasoning_effort"], "medium")
        self.assertEqual(report["max_output_tokens"], 2400)
        self.assertEqual(report["cases_not_run"], [])
        self.assertEqual(report["summary"]["cases_with_accepted_answers"], 5)
        self.assertEqual(report["summary"]["cases_meeting_automated_expectations"], 5)
        self.assertEqual(report["usage"]["model_requests"], create.await_count)
        self.assertEqual(report["usage"]["reported_total_tokens"], create.await_count * 25)
        self.assertEqual(len(report["dataset_sha256"]), 64)
        self.assertEqual(len(report["agent_code_sha256"]), 64)
        self.assertIsNotNone(report["finished_at"])
        self.assertNotIn("offline-test-key", raw_report)
        self.assertNotIn("OFFLINE_GITHUB_CREDENTIAL_CANARY", raw_report)
        for call in create.call_args_list:
            self.assertEqual(call.kwargs["reasoning"], {"effort": "medium"})
            request = json.dumps(call.kwargs)
            self.assertNotIn("reference_notes", request)
            self.assertNotIn("review_questions", request)
            self.assertNotIn("required_labels", request)

    async def test_timeout_keeps_partial_evidence_and_runs_remaining_cases(self):
        async def timeout_run(run):
            run.result = reference_result()
            run.result.content = None
            raise TimeoutError

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(evaluate, "OUTPUT_ROOT", Path(temporary)),
            patch.object(evaluate.Settings, "from_env", return_value=SETTINGS),
            patch.object(AgentRun, "run", new=timeout_run),
            redirect_stdout(io.StringIO()),
        ):
            path = await evaluate.main()
            report = json.loads(path.read_text())
        self.assertEqual(len(report["cases"]), 5)
        self.assertTrue(all(case["outcome"] == "timeout" for case in report["cases"]))
        self.assertTrue(report["cases"][0]["result"]["sources"])
        self.assertEqual(report["summary"]["cases_with_accepted_answers"], 0)

    async def test_authentication_error_stops_remaining_requests_without_leaking_error_body(self):
        error = AuthenticationError("UPSTREAM_SECRET_CANARY", response=httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/responses")), body={"secret": "UPSTREAM_SECRET_CANARY"})
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(evaluate, "OUTPUT_ROOT", Path(temporary)),
            patch.object(evaluate.Settings, "from_env", return_value=SETTINGS),
            patch.object(AgentRun, "run", new=AsyncMock(side_effect=error)) as run,
            redirect_stdout(io.StringIO()),
        ):
            path = await evaluate.main()
            raw_report = path.read_text()
            report = json.loads(raw_report)
        self.assertEqual(run.await_count, 1)
        self.assertEqual(len(report["cases_not_run"]), 4)
        self.assertEqual(report["cases"][0]["error_type"], "AuthenticationError")
        self.assertNotIn("UPSTREAM_SECRET_CANARY", raw_report)
        self.assertEqual(report["summary"]["cases_meeting_automated_expectations"], 0)


if __name__ == "__main__":
    unittest.main()
