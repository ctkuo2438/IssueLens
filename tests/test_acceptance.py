"""Offline acceptance-runner checks; no real GitHub or model requests occur."""

import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import AuthenticationError
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.models import AnalysisContent, AnalysisResult, ContextBuildReport, IssueTarget
from scripts import acceptance
from test_mvp import final_content


def manifest():
    return {
        "id": "offline-acceptance", "selected_at": "2026-11-09T00:00:00Z",
        "selection_source": "fictional", "annotation_status": "pending", "scope": "Fictional offline test cases.",
        "cases": [{
            "id": f"offline_{index}", "kind": kind,
            "issue_url": f"https://github.com/example/project/issues/{index}",
            "selection_reason": "Hidden selection notes.",
            "review_checks": ["Hidden review criterion, never model evidence."],
        } for index, kind in enumerate(("complete_bug_report", "insufficient_information_report", "feature_request"), 1)],
    }


class AcceptanceChecksTests(unittest.TestCase):
    def test_future_reserve_is_counted_and_must_meet_the_next_action_minimum(self):
        report = ContextBuildReport(
            request_number=1, budget_tokens=12000, estimated_tokens_before=9000,
            estimated_tokens=9000, margin_tokens=1024, repair_reserve_tokens=256,
            future_reserve_tokens=2400, future_reserve_target_tokens=2912,
            future_reserve_minimum_tokens=2400, fits=True, sent=True,
        )
        result = AnalysisResult(context_builds=[report])
        result.usage.model_requests = 1
        self.assertTrue(acceptance.functional_checks(result)["dispatched_contexts_within_budget"])
        report.future_reserve_tokens = 3000
        self.assertFalse(acceptance.functional_checks(result)["dispatched_contexts_within_budget"])
        report.future_reserve_tokens = 2399
        self.assertFalse(acceptance.functional_checks(result)["dispatched_contexts_within_budget"])

    def test_old_context_reports_default_to_no_future_reserve(self):
        report = ContextBuildReport.model_validate({
            "request_number": 1, "budget_tokens": 12000, "estimated_tokens_before": 9000,
            "estimated_tokens": 9000, "margin_tokens": 1024, "fits": True,
        })
        self.assertEqual(report.future_reserve_tokens, 0)
        self.assertEqual(report.future_reserve_target_tokens, 0)
        self.assertEqual(report.future_reserve_minimum_tokens, 0)

    def test_missing_final_content_is_not_scored_as_correct_abstention(self):
        checks = acceptance.functional_checks(AnalysisResult())
        self.assertFalse(checks["final_answer_accepted"])
        self.assertFalse(checks["same_repository_search_completed"])
        self.assertIsNone(checks["known_first_person_reproduction_pattern_absent"])
        self.assertIsNone(checks["known_assistant_service_offer_pattern_absent"])
        self.assertIsNone(checks["dispatched_contexts_within_budget"])

    def test_selection_requires_three_distinct_public_issue_urls_and_categories(self):
        acceptance.AcceptanceSet.model_validate(manifest())
        for defect in ("same_url", "same_id", "same_kind", "foreign_host"):
            data = manifest()
            if defect == "foreign_host":
                data["cases"][1]["issue_url"] = "https://example.test/private/secret"
            else:
                key = {"same_url": "issue_url", "same_id": "id", "same_kind": "kind"}[defect]
                data["cases"][1][key] = data["cases"][0][key]
            with self.subTest(defect=defect), self.assertRaises(ValidationError):
                acceptance.AcceptanceSet.model_validate(data)


class AcceptanceRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def run_offline(self, directory, agent_type, settings=None, data=None):
        root = Path(directory)
        cases_path = root / "cases.json"
        selection_path = root / "selection.json"
        cases_path.write_text(json.dumps(data if data is not None else manifest()))
        selection_path.write_text('{"source":"fictional offline selection"}')
        with (
            patch.object(acceptance, "CASES_PATH", cases_path),
            patch.object(acceptance, "SELECTION_PATH", selection_path),
            patch.object(acceptance, "OUTPUT_ROOT", root / "reports"),
            patch.object(acceptance.Settings, "from_env", return_value=settings or Settings(openai_api_key="offline-secret-key", github_token="offline-github-token")) as configure,
            patch.object(acceptance, "AgentRun", agent_type),
            redirect_stdout(io.StringIO()),
        ):
            path = await acceptance.main()
        return path, configure

    async def test_runner_passes_only_target_settings_and_events_then_preserves_review_evidence(self):
        captured = []

        class SimulatedRun:
            def __init__(self, target, settings, emit):
                captured.append((target, settings))
                self.result = AnalysisResult(content=AnalysisContent.model_validate(final_content()))

            async def run(self):
                return self.result

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(openai_api_key="offline-secret-key", github_token="offline-github-token", model="gpt-5.6", reasoning_effort="medium")
            path, _ = await self.run_offline(directory, SimulatedRun, settings)
            report = json.loads(path.read_text())
            self.assertEqual(report["model"], "gpt-5.6")
            self.assertEqual(report["reasoning_effort"], "medium")
            self.assertEqual(report["max_output_tokens"], 2400)
            self.assertEqual(len(captured), 3)
            self.assertTrue(all(isinstance(target, IssueTarget) for target, _ in captured))
            self.assertNotIn("Hidden", repr(captured))
            self.assertEqual(report["cases_not_run"], [])
            self.assertEqual(report["human_review_status"], "pending")
            self.assertTrue(all(case["human_review"]["status"] == "pending" for case in report["cases"]))
            self.assertTrue(all(case["human_review"]["predeclared_checks"] for case in report["cases"]))
            self.assertEqual(json.loads((path.parent / "cases.json").read_text()), manifest())
            self.assertTrue((path.parent / "selection.json").exists())
            self.assertNotIn("offline-secret-key", path.read_text())
            self.assertNotIn("offline-github-token", path.read_text())
            self.assertIn("backend/app/context.py", report["code_sha256"])

    async def test_authentication_failure_stops_remaining_cases_and_sanitizes_upstream_text(self):
        class RejectedRun:
            def __init__(self, target, settings, emit):
                pass

            async def run(self):
                raise AuthenticationError("Sensitive upstream response body.", response=httpx.Response(401, request=httpx.Request("POST", "https://example.test")), body={"secret": "do-not-save"})

            def snapshot(self, reason):
                return AnalysisResult(limitations=[reason])

        with tempfile.TemporaryDirectory() as directory:
            path, _ = await self.run_offline(directory, RejectedRun)
            report = json.loads(path.read_text())
            self.assertEqual(len(report["cases"]), 1)
            self.assertEqual(len(report["cases_not_run"]), 2)
            self.assertEqual(report["cases"][0]["error_type"], "AuthenticationError")
            self.assertNotIn("Sensitive upstream", path.read_text())
            self.assertNotIn("do-not-save", path.read_text())

    async def test_github_access_limit_stops_before_the_next_case(self):
        class LimitedRun:
            def __init__(self, target, settings, emit):
                pass

            async def run(self):
                return AnalysisResult(limitations=["get_issue: GitHub denied access or its rate limit was reached. Check GITHUB_TOKEN or try later."])

        with tempfile.TemporaryDirectory() as directory:
            path, _ = await self.run_offline(directory, LimitedRun)
            report = json.loads(path.read_text())
            self.assertEqual(len(report["cases"]), 1)
            self.assertEqual(len(report["cases_not_run"]), 2)
            self.assertIn("GitHub", report["stop_reason"])

    async def test_timeout_records_missing_answer_without_marking_it_as_passing(self):
        class SlowRun:
            def __init__(self, target, settings, emit):
                pass

            async def run(self):
                await asyncio.sleep(0.03)
                return AnalysisResult()

            def snapshot(self, reason):
                return AnalysisResult(limitations=[reason])

        with tempfile.TemporaryDirectory() as directory:
            path, _ = await self.run_offline(directory, SlowRun, Settings(openai_api_key="offline", runtime_seconds=0.001))
            report = json.loads(path.read_text())
            self.assertEqual(len(report["cases"]), 3)
            self.assertTrue(all(case["outcome"] == "timeout" for case in report["cases"]))
            self.assertIsNone(report["reasoning_effort"])
            self.assertTrue(all(case["functional_checks"]["final_answer_accepted"] is False for case in report["cases"]))
