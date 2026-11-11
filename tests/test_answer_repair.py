"""Offline citation and bounded answer-regeneration tests with simulated providers."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from openai.types.responses import ResponseOutputMessage, ResponseOutputText, ResponseReasoningItem

from backend.app.agent import CANDIDATE_RESERVE_TOKENS, REPAIR_RESERVE_TOKENS
from backend.app.context import ContextBuilder
from backend.app.main import create_app
from test_context import build, exchange, source
from test_labels import label_content, label_github_response, label_prefix
from test_m2 import investigation_prefix, m2_content, m2_github_response
from test_mvp import OfflineEncoding, SETTINGS, commented_github_response, final_content, run_simulated_agent, simulated_response, tool_call
from test_required_lookups import PolicyServer, content_with_comparison, prefix as policy_prefix, run_policy, step


def invalid_citations():
    content = label_content()
    content["label_recommendations"][0]["source_ids"] = ["S1"]
    return content


class RepairReserveTests(unittest.TestCase):
    def test_reserve_fits_inside_original_budget_without_advancing_a_failed_build(self):
        history = exchange("get_issue", "issue", {"source": source("S1", "issue", "Protected issue body.")})
        probe = build(ContextBuilder(len, 10000), history)
        budget = probe.report.estimated_tokens + 100
        builder = ContextBuilder(len, budget)
        rejected = builder.build(history, instructions="Read only retrieved evidence.", tools=[], text_format={}, request_number=1, focus="", repair_reserve_tokens=256)
        self.assertFalse(rejected.report.fits)
        self.assertLessEqual(rejected.report.estimated_tokens, budget)
        self.assertEqual(rejected.report.repair_reserve_tokens, 256)
        self.assertEqual(builder.visible_source_ids, set())
        final = build(builder, history)
        self.assertTrue(final.report.fits)
        self.assertEqual(builder.visible_source_ids, {"S1"})

    def test_reserve_can_omit_only_new_optional_evidence_and_keeps_original_prefix(self):
        original = exchange("get_issue", "issue", {"source": source("S1", "issue", "Protected evidence.")})
        optional = exchange("get_repo_readme", "readme", {"source": source("S2", "readme", "General background. " * 50)})
        probe = build(ContextBuilder(len, 10000), original + optional)
        builder = ContextBuilder(len, probe.report.estimated_tokens + 20)
        first = build(builder, original)
        prefix = deepcopy(first.history)
        second = builder.build(first.history + optional, instructions="Read only retrieved evidence.", tools=[], text_format={}, request_number=2, focus="", repair_reserve_tokens=300)
        self.assertTrue(second.report.fits)
        self.assertEqual(second.history[:len(prefix)], prefix)
        self.assertEqual(json.loads(second.history[-1]["output"])["source"]["excerpt"], "")
        self.assertLessEqual(second.report.estimated_tokens + second.report.repair_reserve_tokens, second.report.budget_tokens)

    def test_negative_reserve_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "negative"):
            ContextBuilder(len, 5000).build([], instructions="", tools=[], text_format={}, request_number=1, focus="", repair_reserve_tokens=-1)


class CitationRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_source_roles_are_explicit_before_the_final_answer(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=label_content()),
        ], handler=label_github_response)
        instructions = create.call_args.kwargs["instructions"]
        self.assertIn('"target_issue": "S1"', instructions)
        self.assertIn('"label_recommendations": ["S1", "S2"]', instructions)
        self.assertEqual(result.content.label_recommendations[0].source_ids, ["S1", "S2"])

    async def test_candidate_citation_roles_use_the_full_body_not_the_search_hit(self):
        content = m2_content()
        content["comparisons"] = content["comparisons"][:1]
        result, create, _ = await run_simulated_agent([
            *investigation_prefix((41,)), simulated_response(content=content),
        ], handler=m2_github_response)
        self.assertIsNotNone(result.content)
        self.assertIn('"comparisons": {"41": ["S1", "S3"]}', create.call_args.kwargs["instructions"])

    async def test_repair_uses_the_same_evidence_without_replaying_a_large_rejected_answer(self):
        probe, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=label_content()),
        ], handler=label_github_response)
        budget = probe.context_builds[-1].estimated_tokens + REPAIR_RESERVE_TOKENS + 32
        bad = invalid_citations()
        bad["draft_reply"] = "Rejected draft detail. " * 1000
        reasoning = ResponseReasoningItem(type="reasoning", id="rs_rejected", summary=[], encrypted_content="opaque-rejected-reasoning")
        message = ResponseOutputMessage(type="message", id="msg_rejected", role="assistant", status="completed", content=[ResponseOutputText(type="output_text", text=json.dumps(bad), annotations=[])])
        before = [reasoning.to_dict(), message.to_dict()]
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response([reasoning, message], content=bad), simulated_response(content=label_content()),
        ], settings=replace(SETTINGS, context_tokens=budget), handler=label_github_response)
        self.assertIsNotNone(result.content)
        first = create.call_args_list[3].kwargs
        repaired = create.call_args_list[4].kwargs
        self.assertEqual(first["input"], repaired["input"])
        self.assertEqual(repaired["tool_choice"], "none")
        self.assertNotIn("Rejected draft detail", json.dumps(repaired))
        self.assertIn("Required IDs: ['S1', 'S2']; received: ['S1']", repaired["instructions"])
        self.assertEqual([reasoning.to_dict(), message.to_dict()], before)
        self.assertEqual(result.rejected_answers[0].output_text, json.dumps(bad))
        self.assertRegex(result.rejected_answers[0].output_sha256, r"^[a-f0-9]{64}$")
        self.assertEqual(result.context_builds[-1].phase, "repair")
        self.assertEqual(result.context_builds[-1].future_reserve_tokens, 0)
        self.assertTrue(all(report.estimated_tokens + report.repair_reserve_tokens + report.future_reserve_tokens <= budget for report in result.context_builds if report.sent))
        legacy_replay_size = len(json.dumps([first["instructions"], first["tools"], first["text"], first["input"] + before], ensure_ascii=False).encode()) + 1024
        self.assertGreater(legacy_replay_size, budget)

    async def test_repeated_bad_answers_do_not_accumulate_history_or_feedback(self):
        first = invalid_citations()
        second = invalid_citations()
        second["label_recommendations"][0]["source_ids"] = ["S1", *[f"unseen-{i}" for i in range(500)]]
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=first), simulated_response(content=second),
            simulated_response(content=label_content()),
        ], handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(len(result.rejected_answers), 2)
        self.assertEqual(create.call_args_list[3].kwargs["input"], create.call_args_list[4].kwargs["input"])
        self.assertEqual(create.call_args_list[4].kwargs["input"], create.call_args_list[5].kwargs["input"])
        self.assertNotIn("unseen-499", create.call_args.kwargs["instructions"])
        for answer in result.rejected_answers:
            serialized = json.dumps(f"\nFinal-answer repair feedback: {answer.feedback}", ensure_ascii=False)
            self.assertLessEqual(len(OfflineEncoding().encode(serialized)), REPAIR_RESERVE_TOKENS)
        self.assertEqual(result.usage.tool_calls, 3)

    async def test_a_fitting_final_attempt_is_allowed_when_the_full_reserve_does_not_fit(self):
        probe, _, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=label_content()),
        ], handler=label_github_response)
        budget = probe.context_builds[-1].estimated_tokens + 8
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=label_content()),
        ], settings=replace(SETTINGS, context_tokens=budget), handler=label_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertFalse(result.context_builds[-2].fits)
        self.assertEqual(result.context_builds[-2].repair_reserve_tokens, REPAIR_RESERVE_TOKENS)
        self.assertTrue(result.context_builds[-1].fits)
        self.assertEqual(result.context_builds[-1].repair_reserve_tokens, 0)
        self.assertTrue(any("without a full repair reserve" in reason for reason in result.limitations))

    async def test_repair_does_not_execute_an_unexpected_tool_request(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=invalid_citations()),
            simulated_response([tool_call("get_repo_labels", call_id="unexpected")]),
        ], handler=label_github_response)
        self.assertIsNone(result.content)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertTrue(any("no tool was executed" in reason for reason in result.limitations))

    async def test_context_finalization_reports_unread_comments_instead_of_forcing_an_unfitting_read(self):
        prefix = simulated_response([tool_call("get_issue")])
        probe, _, _ = await run_simulated_agent([
            prefix, simulated_response([tool_call("get_issue_comments", '{"page":1}', "comments")]),
            simulated_response(content=final_content()),
        ], handler=commented_github_response)
        budget = probe.context_builds[1].estimated_tokens + 8
        result, create, _ = await run_simulated_agent([
            prefix, simulated_response(content=final_content()),
        ], settings=replace(SETTINGS, context_tokens=budget), handler=commented_github_response)
        self.assertIsNotNone(result.content)
        self.assertEqual(result.comment_check.status, "not_read")
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertNotIn("The required next action is get_issue_comments", create.call_args.kwargs["instructions"])
        self.assertTrue(any("no comments lookup" in reason for reason in result.limitations))

    async def test_iteration_exhaustion_keeps_the_rejected_candidate_and_sources(self):
        result, create, _ = await run_simulated_agent([
            *label_prefix(), simulated_response(content=invalid_citations()),
            simulated_response(content=invalid_citations()),
        ], settings=replace(SETTINGS, max_iterations=5), handler=label_github_response)
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 5)
        self.assertEqual(len(result.rejected_answers), 2)
        self.assertTrue(result.sources)


class RepairTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def candidate_gate_budget(self):
        probe, _, _ = await run_policy([
            *policy_prefix(), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        self.assertIsNotNone(probe.content)
        return probe.context_builds[3].estimated_tokens + CANDIDATE_RESERVE_TOKENS - 100

    async def test_candidate_pending_repair_keeps_the_same_evidence_and_zero_future_reserve(self):
        settings = replace(SETTINGS, context_tokens=await self.candidate_gate_budget(), model="gpt-5.6", reasoning_effort="medium")
        server = PolicyServer()
        bad = final_content("S999")
        before = deepcopy(bad)
        result, create, emit = await run_policy([
            *policy_prefix(), simulated_response(content=bad), simulated_response(content=final_content()),
        ], server, settings)
        self.assertIsNotNone(result.content)
        self.assertEqual(create.await_count, 5)
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(server.candidate_calls, 0)
        self.assertEqual(result.similar_issue_check.candidate_status, "not_checked")
        self.assertEqual(result.similar_issue_check.attempted_candidate_numbers, [])
        self.assertEqual(result.content.comparisons, [])
        self.assertEqual(create.call_args_list[-2].kwargs["input"], create.call_args.kwargs["input"])
        self.assertEqual(create.call_args_list[-2].kwargs["tool_choice"], "none")
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertNotIn("The required next action is", create.call_args.kwargs["instructions"])
        repair = result.context_builds[-1]
        self.assertEqual(repair.phase, "repair")
        self.assertTrue(repair.fits and repair.sent)
        self.assertEqual((repair.future_reserve_tokens, repair.future_reserve_target_tokens, repair.future_reserve_minimum_tokens), (0, 0, 0))
        self.assertLessEqual(repair.estimated_tokens, settings.context_tokens)
        self.assertEqual(len(result.rejected_answers), 1)
        self.assertEqual(result.rejected_answers[0].output_text, json.dumps(bad))
        self.assertEqual(bad, before)
        self.assertTrue(any("Finalizing early" in event.args[1] for event in emit.call_args_list))
        self.assertTrue(all(call.kwargs["model"] == "gpt-5.6" and call.kwargs["reasoning"] == {"effort": "medium"} and call.kwargs["max_output_tokens"] == 2400 for call in create.call_args_list))

    async def test_pending_comments_catalog_and_search_cannot_override_repair(self):
        for pending, comments, prefix_length, build_index in (
            ("comments", 1, 1, 1), ("catalog", 0, 1, 1), ("search", 0, 2, 2),
        ):
            with self.subTest(pending=pending):
                full_prefix = [step("get_issue")]
                if comments:
                    full_prefix.append(step("get_issue_comments", page=1))
                full_prefix.extend([step("get_repo_labels"), step("search_issues", query="export crash")])
                probe, _, _ = await run_policy([
                    *full_prefix, step("get_related_issue", issue_number=41),
                    simulated_response(content=content_with_comparison("S5" if comments else "S4")),
                ], PolicyServer(comments=comments))
                budget = probe.context_builds[build_index].estimated_tokens + REPAIR_RESERVE_TOKENS + 128
                server = PolicyServer(comments=comments)
                result, create, _ = await run_policy([
                    *full_prefix[:prefix_length], simulated_response(content=final_content("S999")),
                    simulated_response(content=final_content()),
                ], server, replace(SETTINGS, context_tokens=budget))
                self.assertIsNotNone(result.content)
                self.assertEqual(create.await_count, prefix_length + 2)
                self.assertEqual(len(server.requests), prefix_length)
                self.assertEqual(result.usage.tool_calls, prefix_length)
                self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
                self.assertNotIn("The required next action is", create.call_args.kwargs["instructions"])
                self.assertEqual(create.call_args_list[-2].kwargs["input"], create.call_args.kwargs["input"])
                self.assertEqual(result.context_builds[-1].phase, "repair")
                self.assertEqual(result.context_builds[-1].future_reserve_target_tokens, 0)
                self.assertEqual(result.context_builds[-1].future_reserve_minimum_tokens, 0)
                self.assertEqual(result.similar_issue_check.search_status, "not_checked")
                if pending == "comments":
                    self.assertEqual(result.comment_check.status, "not_read")
                    self.assertEqual(result.comment_check.attempted_pages, [])
                if pending != "search":
                    self.assertIsNone(result.labels_source_id)

    async def test_unexpected_candidate_tool_during_repair_is_not_executed(self):
        server = PolicyServer()
        result, create, _ = await run_policy([
            *policy_prefix(), simulated_response(content=final_content("S999")),
            step("get_related_issue", issue_number=41),
        ], server, replace(SETTINGS, context_tokens=await self.candidate_gate_budget()))
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 5)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "none")
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(server.candidate_calls, 0)
        self.assertEqual(result.similar_issue_check.attempted_candidate_numbers, [])
        self.assertEqual(len(result.rejected_answers), 1)
        self.assertTrue(any("during final-answer repair; no tool was executed" in item for item in result.limitations))

    async def test_repeated_repairs_stop_at_the_original_iteration_limit(self):
        server = PolicyServer()
        result, create, _ = await run_policy([
            *policy_prefix(), *[simulated_response(content=final_content("S999")) for _ in range(3)],
        ], server, replace(SETTINGS, context_tokens=await self.candidate_gate_budget(), max_iterations=6))
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 6)
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertEqual(server.candidate_calls, 0)
        self.assertEqual(len(result.rejected_answers), 3)
        repairs = [item for item in result.context_builds if item.phase == "repair"]
        self.assertEqual(len(repairs), 2)
        self.assertTrue(all(item.sent and item.future_reserve_target_tokens == 0 for item in repairs))
        self.assertEqual(create.call_args_list[-3].kwargs["input"], create.call_args_list[-2].kwargs["input"])
        self.assertEqual(create.call_args_list[-2].kwargs["input"], create.call_args.kwargs["input"])
        self.assertTrue(result.sources)

    async def test_repair_input_that_really_exceeds_budget_still_stops_before_request(self):
        probe, _, _ = await run_policy([
            *policy_prefix(), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], PolicyServer())
        budget = probe.context_builds[1].estimated_tokens + 8
        server = PolicyServer()
        result, create, _ = await run_policy([
            step("get_issue"), simulated_response(content=final_content("S999")),
        ], server, replace(SETTINGS, context_tokens=budget))
        self.assertIsNone(result.content)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(len(server.requests), 1)
        repair = result.context_builds[-1]
        self.assertEqual(repair.phase, "repair")
        self.assertEqual((repair.future_reserve_tokens, repair.future_reserve_target_tokens, repair.future_reserve_minimum_tokens), (0, 0, 0))
        self.assertGreater(repair.estimated_tokens, budget)
        self.assertFalse(repair.fits or repair.sent)
        self.assertEqual(len(result.rejected_answers), 1)
        self.assertTrue(result.sources)

    async def test_premature_answers_still_retry_required_lookups_as_investigation(self):
        for name, comments, index in (
            ("get_issue_comments", 1, 1), ("get_repo_labels", 0, 1),
            ("search_issues", 0, 2), ("get_related_issue", 0, 3),
        ):
            with self.subTest(required_lookup=name):
                planned = [step("get_issue")]
                if comments:
                    planned.append(step("get_issue_comments", page=1))
                planned.extend([
                    step("get_repo_labels"), step("search_issues", query="export crash"),
                    step("get_related_issue", issue_number=41),
                ])
                result, create, emit = await run_policy([
                    *planned[:index], simulated_response(content=final_content()), *planned[index:],
                    simulated_response(content=content_with_comparison("S5" if comments else "S4")),
                ], PolicyServer(comments=comments))
                self.assertIsNotNone(result.content)
                self.assertEqual(len(result.rejected_answers), 1)
                self.assertIn(f"Complete {name}", result.rejected_answers[0].reason)
                retried = create.call_args_list[index + 1].kwargs
                self.assertEqual(retried["tool_choice"], {"type": "function", "name": name})
                self.assertNotIn("Final-answer repair feedback", retried["instructions"])
                self.assertEqual(create.call_args_list[index].kwargs["input"], retried["input"])
                report = next(item for item in result.context_builds if item.request_number == index + 2 and item.sent)
                self.assertEqual(report.phase, "investigation")
                self.assertEqual(result.similar_issue_check.comparison_status, "compared")
                self.assertTrue(any("Retrying the required lookup" in event.args[1] for event in emit.call_args_list))


class RepairAPITests(unittest.TestCase):
    def test_rejected_candidates_and_context_phases_survive_api_serialization(self):
        result, _, _ = asyncio.run(run_simulated_agent([
            *label_prefix(), simulated_response(content=invalid_citations()), simulated_response(content=label_content()),
        ], handler=label_github_response))
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=result)):
            with TestClient(create_app(SETTINGS)) as client:
                created = client.post("/api/v1/analyses", json={"issue_url": "https://github.com/example/project/issues/42"})
                job_id = created.json()["job_id"]
                client.get(f"/api/v1/analyses/{job_id}/events")
                data = client.get(f"/api/v1/analyses/{job_id}").json()["result"]
                self.assertEqual(data["content"]["label_recommendations"][0]["source_ids"], ["S1", "S2"])
                self.assertEqual(data["rejected_answers"][0]["output_text"], json.dumps(invalid_citations()))
                self.assertEqual(data["context_builds"][-1]["phase"], "repair")


if __name__ == "__main__":
    unittest.main()
