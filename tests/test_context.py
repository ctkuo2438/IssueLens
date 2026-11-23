"""Offline context checks; both model decisions and GitHub data are simulated."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest

import httpx
from openai.types.responses import ResponseReasoningItem

from backend.app.agent import CANDIDATE_RESERVE_TOKENS, REPAIR_RESERVE_TOKENS
from backend.app.context import ContextBuilder, REQUEST_MARGIN_TOKENS
from test_mvp import ISSUE, SETTINGS, final_content, github_response, run_simulated_agent, simulated_response, tool_call


def source(source_id, kind, excerpt):
    return {"id": source_id, "kind": kind, "title": "Fictional evidence", "url": f"https://example.test/{source_id}", "excerpt": excerpt}


def exchange(name, call_id, payload):
    return [
        tool_call(name, call_id=call_id).to_dict(),
        {"type": "function_call_output", "call_id": call_id, "output": json.dumps(payload)},
    ]


def build(builder, history, number=1):
    return builder.build(history, instructions="Read only retrieved evidence.", tools=[], text_format={}, request_number=number, focus="Export fails on Windows")


class ContextBuilderTests(unittest.TestCase):
    def test_fitting_input_retains_evidence_without_mutating_original(self):
        original = [{"role": "user", "content": "Investigate the fictional issue."}, *exchange("get_issue", "issue", {"source": source("S1", "issue", "Export fails.")})]
        before = deepcopy(original)
        builder = ContextBuilder(len, 5000)
        result = build(builder, original)
        self.assertTrue(result.report.fits)
        self.assertFalse(result.report.sent)
        self.assertEqual(json.loads(result.history[-1]["output"])["source"], json.loads(before[-1]["output"])["source"])
        self.assertEqual(original, before)
        self.assertEqual(builder.visible_source_ids, {"S1"})
        self.assertEqual(result.report.margin_tokens, REQUEST_MARGIN_TOKENS)

    def test_lower_priority_comment_is_omitted_before_matching_maintainer_comment(self):
        payload = {"page": 1, "has_next_page": True, "max_page": 5, "comments": [
            source("S2", "comment", "Author association: NONE\n" + "Unrelated greeting. " * 75),
            source("S3", "comment", "Author association: MEMBER\n" + "Export fails on Windows. " * 60),
        ]}
        original = exchange("get_issue_comments", "comments", payload)
        result = build(ContextBuilder(len, 3500), original)
        self.assertTrue(result.report.fits)
        packed = json.loads(result.history[-1]["output"])
        self.assertEqual(packed["comments"][0]["excerpt"], "")
        self.assertEqual(packed["comments"][1]["excerpt"], payload["comments"][1]["excerpt"])
        self.assertTrue(packed["has_next_page"])
        self.assertEqual(packed["page"], 1)
        self.assertEqual(packed["context_selection"]["omitted_excerpt_source_ids"], ["S2"])
        self.assertEqual(json.loads(original[-1]["output"]), payload)
        self.assertLessEqual(result.report.estimated_tokens, result.report.budget_tokens)

    def test_target_candidate_and_label_excerpts_are_never_cut_to_fit(self):
        for kind, name in [("issue", "get_issue"), ("related_issue", "get_related_issue"), ("repo_labels", "get_repo_labels")]:
            with self.subTest(kind=kind):
                payload = {"source": source("S1", kind, "Essential complete evidence. " * 150)}
                builder = ContextBuilder(len, 2200)
                result = build(builder, exchange(name, "protected", payload))
                self.assertFalse(result.report.fits)
                self.assertEqual(json.loads(result.history[-1]["output"]), payload)
                self.assertEqual(builder.visible_source_ids, set())
                self.assertFalse(result.report.sent)

    def test_frozen_prefix_and_encrypted_reasoning_survive_new_result_packing(self):
        reasoning = ResponseReasoningItem(id="rs_context", type="reasoning", summary=[], encrypted_content="opaque-offline-reasoning").to_dict()
        builder = ContextBuilder(len, 4200)
        first = build(builder, [{"role": "user", "content": "Investigate."}, reasoning, *exchange("get_issue", "issue", {"source": source("S1", "issue", "Export fails.")})])
        prefix = deepcopy(first.history)
        next_history = first.history + exchange("get_repo_readme", "readme", {"source": source("S2", "readme", "General project text. " * 300)})
        second = build(builder, next_history, 2)
        self.assertTrue(second.report.fits)
        self.assertEqual(second.history[:len(prefix)], prefix)
        self.assertEqual(second.history[1], reasoning)
        self.assertEqual(second.history[-2]["call_id"], second.history[-1]["call_id"])
        self.assertEqual(second.history[-1]["call_id"], "readme")
        self.assertEqual(json.loads(second.history[-1]["output"])["source"]["excerpt"], "")
        self.assertEqual(builder.visible_source_ids, {"S1"})

    def test_rewriting_a_previously_sent_prefix_is_rejected(self):
        builder = ContextBuilder(len, 5000)
        first = build(builder, [{"role": "user", "content": "Original issue."}])
        first.history[0]["content"] = "Changed issue."
        with self.assertRaisesRegex(ValueError, "prefix"):
            build(builder, first.history, 2)

    def test_exact_repeated_result_reuses_the_earlier_call_without_duplicate_text(self):
        builder = ContextBuilder(len, 8000)
        payload = {"source": source("S1", "issue", "Export failure evidence. " * 50)}
        first = build(builder, exchange("get_issue", "first", payload))
        second = build(builder, first.history + exchange("get_issue", "again", payload), 2)
        repeated = json.loads(second.history[-1]["output"])
        self.assertEqual(repeated["context_reuse"], {"call_id": "first", "source_ids": ["S1"]})
        self.assertEqual(second.history[-1]["call_id"], "again")
        self.assertEqual(second.report.reused_call_ids, ["first"])
        self.assertLess(second.report.estimated_tokens, second.report.estimated_tokens_before)
        self.assertEqual(builder.visible_source_ids, {"S1"})

    def test_changed_payload_is_not_mistaken_for_a_cached_result(self):
        builder = ContextBuilder(len, 8000)
        first = build(builder, exchange("get_issue", "first", {"source": source("S1", "issue", "Old evidence.")}))
        second = build(builder, first.history + exchange("get_issue", "changed", {"source": source("S1", "issue", "Changed evidence.")}), 2)
        self.assertEqual(second.report.reused_call_ids, [])
        self.assertEqual(json.loads(second.history[-1]["output"])["source"]["excerpt"], "Changed evidence.")

    def test_tiny_repeated_error_does_not_grow_into_a_larger_reference(self):
        builder = ContextBuilder(len, 5000)
        payload = {"error": "Unavailable."}
        first = build(builder, exchange("get_repo_readme", "first", payload))
        second = build(builder, first.history + exchange("get_repo_readme", "again", payload), 2)
        self.assertEqual(second.report.reused_call_ids, [])
        self.assertEqual(json.loads(second.history[-1]["output"]), payload)
        self.assertLessEqual(second.report.estimated_tokens, second.report.estimated_tokens_before)

    def test_tiny_excerpt_is_not_removed_when_its_notice_would_cost_more(self):
        payload = {"source": source("S1", "readme", "Brief text.")}
        result = build(ContextBuilder(len, 1300), exchange("get_repo_readme", "readme", payload))
        self.assertFalse(result.report.fits)
        self.assertEqual(result.report.sources[0].action, "kept")
        self.assertEqual(json.loads(result.history[-1]["output"]), payload)
        self.assertLessEqual(result.report.estimated_tokens, result.report.estimated_tokens_before)

    def test_reusing_an_omitted_excerpt_does_not_make_it_visible(self):
        builder = ContextBuilder(len, 2300)
        payload = {"source": source("S1", "readme", "General background. " * 200)}
        first = build(builder, exchange("get_repo_readme", "first", payload))
        self.assertTrue(first.report.fits)
        self.assertEqual(first.report.sources[0].action, "omitted")
        second = build(builder, first.history + exchange("get_repo_readme", "again", payload), 2)
        self.assertTrue(second.report.fits)
        self.assertEqual(second.report.sources[0].action, "reused")
        self.assertEqual(builder.visible_source_ids, set())
        self.assertIn("omissions still apply", json.loads(second.history[-1]["output"])["note"])

    def test_failed_build_does_not_register_unsent_results_as_reusable(self):
        builder = ContextBuilder(len, 100)
        history = exchange("get_issue", "first", {"source": source("S1", "issue", "Export failure.")})
        failed = build(builder, history)
        self.assertFalse(failed.report.fits)
        builder.budget_tokens = 5000
        retry = build(builder, history)
        self.assertTrue(retry.report.fits)
        self.assertEqual(retry.report.reused_call_ids, [])
        self.assertEqual(json.loads(retry.history[-1]["output"])["source"]["excerpt"], "Export failure.")

    def test_template_fields_and_unknown_required_state_survive_raw_excerpt_omission(self):
        fields = [{"label": "Feature purpose", "required": None, "required_options": [], "kind": "markdown", "description": "Describe the proposed behavior."}]
        payload = {"complete": False, "templates": [{"source_id": "S2", "fields": fields, "requirements_known": False}], "sources": [source("S2", "issue_template", "Untrusted template text. " * 200)]}
        builder = ContextBuilder(len, 2400)
        result = build(builder, exchange("get_issue_templates", "templates", payload))
        self.assertTrue(result.report.fits)
        packed = json.loads(result.history[-1]["output"])
        self.assertEqual(packed["templates"], payload["templates"])
        self.assertFalse(packed["complete"])
        self.assertEqual(packed["sources"][0]["excerpt"], "")
        self.assertEqual(builder.visible_source_ids, {"S2"})

    def test_structured_search_preserves_empty_and_incomplete_states(self):
        search = {"query": "repo:example/project export", "total_count": 0, "candidate_numbers": [], "incomplete_results": True, "source_id": "S2"}
        payload = {"search": search, "candidates": [], "source": source("S2", "search", json.dumps({key: value for key, value in search.items() if key != "source_id"}))}
        result = build(ContextBuilder(len, 4000), exchange("search_issues", "search", payload))
        packed = json.loads(result.history[-1]["output"])
        self.assertEqual(packed["search"], search)
        self.assertEqual(packed["candidates"], [])
        self.assertEqual(result.report.sources[0].action, "redundant")

    def test_fixed_instructions_tools_and_schema_are_charged_to_the_budget(self):
        builder = ContextBuilder(len, 1400)
        result = builder.build([], instructions="Rules. " * 300, tools=[], text_format={"format": {"type": "text"}}, request_number=1, focus="")
        self.assertFalse(result.report.fits)
        self.assertGreater(result.report.estimated_tokens, 1400)

    def test_exact_estimated_budget_boundary_is_allowed(self):
        history = exchange("get_issue", "issue", {"source": source("S1", "issue", "Export failure.")})
        probe = build(ContextBuilder(len, 5000), history)
        boundary = probe.report.estimated_tokens
        self.assertTrue(build(ContextBuilder(len, boundary), history).report.fits)
        self.assertFalse(build(ContextBuilder(len, boundary - 1), history).report.fits)


class FutureReserveTests(unittest.TestCase):
    def test_future_and_repair_reserves_share_the_existing_budget_at_the_boundary(self):
        history = exchange("get_issue", "issue", {"source": source("S1", "issue", "Protected evidence.")})
        estimate = build(ContextBuilder(len, 10000), history).report.estimated_tokens
        for difference, fits in ((0, True), (-1, False)):
            with self.subTest(difference=difference):
                builder = ContextBuilder(len, estimate + 900 + 256 + difference)
                result = builder.build(
                    history, instructions="Read only retrieved evidence.", tools=[], text_format={},
                    request_number=1, focus="", repair_reserve_tokens=256, future_reserve_tokens=900,
                )
                self.assertEqual(result.report.fits, fits)
                self.assertEqual(result.report.future_reserve_target_tokens, 900)
                self.assertEqual(result.report.future_reserve_tokens, 900 + difference)
                self.assertEqual(result.report.estimated_tokens, estimate)
                self.assertEqual(builder.visible_source_ids, {"S1"} if fits else set())

    def test_early_omission_makes_a_later_candidate_fit_without_rewriting_the_prefix(self):
        original = exchange("get_issue", "issue", {"source": source("S1", "issue", "Export failure.")})
        comments = exchange("get_issue_comments", "comments", {"comments": [
            source("S2", "comment", "Author association: NONE\n" + "Unrelated background. " * 75),
            source("S3", "comment", "Author association: MEMBER\n" + "Export fails on Windows. " * 50),
        ]})
        candidate = exchange("get_related_issue", "candidate", {"source": source("S4", "related_issue", "Candidate evidence. " * 50)})
        estimate = build(ContextBuilder(len, 20000), original + comments).report.estimated_tokens
        budget = estimate + 200
        without_reserve = ContextBuilder(len, budget)
        old = build(without_reserve, original + comments)
        self.assertFalse(build(without_reserve, old.history + candidate, 2).report.fits)

        builder = ContextBuilder(len, budget)
        first = build(builder, original)
        packed = builder.build(
            first.history + comments, instructions="Read only retrieved evidence.", tools=[], text_format={},
            request_number=2, focus="Export fails on Windows", future_reserve_tokens=1500,
        )
        self.assertTrue(packed.report.fits)
        self.assertEqual(packed.history[:len(first.history)], first.history)
        self.assertEqual([item.source_id for item in packed.report.sources if item.action == "omitted"], ["S2"])
        final = build(builder, packed.history + candidate, 3)
        self.assertTrue(final.report.fits)
        self.assertEqual(final.history[:len(packed.history)], packed.history)
        self.assertEqual(json.loads(final.history[-1]["output"])["source"]["excerpt"], json.loads(candidate[-1]["output"])["source"]["excerpt"])
        self.assertNotIn("S2", builder.visible_source_ids)
        self.assertIn("S4", builder.visible_source_ids)
        self.assertTrue(json.loads(comments[-1]["output"])["comments"][0]["excerpt"])

    def test_failed_future_reserve_does_not_freeze_or_reuse_unsent_evidence(self):
        history = exchange("get_issue", "issue", {"source": source("S1", "issue", "Protected evidence.")})
        estimate = build(ContextBuilder(len, 10000), history).report.estimated_tokens
        builder = ContextBuilder(len, estimate + 10)
        blocked = builder.build(
            history, instructions="Read only retrieved evidence.", tools=[], text_format={},
            request_number=1, focus="", future_reserve_tokens=100,
        )
        self.assertFalse(blocked.report.fits)
        self.assertEqual(builder.visible_source_ids, set())
        final = build(builder, history)
        self.assertTrue(final.report.fits)
        self.assertEqual(final.report.reused_call_ids, [])
        self.assertEqual(final.report.future_reserve_tokens, 0)

    def test_negative_future_reserve_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "negative"):
            ContextBuilder(len, 5000).build(
                [], instructions="", tools=[], text_format={}, request_number=1, focus="", future_reserve_tokens=-1,
            )

    def test_partial_target_can_use_remaining_room_after_omitting_new_optional_text(self):
        history = exchange("get_issue", "issue", {"source": source("S1", "issue", "Protected evidence.")})
        history += exchange("get_repo_readme", "readme", {"source": source("S2", "readme", "Background. " * 100)})
        probe = ContextBuilder(len, 10000).build(
            history, instructions="Read only retrieved evidence.", tools=[], text_format={},
            request_number=1, focus="", future_reserve_tokens=10000,
        )
        builder = ContextBuilder(len, probe.report.estimated_tokens + 600)
        result = builder.build(
            history, instructions="Read only retrieved evidence.", tools=[], text_format={},
            request_number=1, focus="", repair_reserve_tokens=256,
            future_reserve_tokens=2000, minimum_future_reserve_tokens=300,
        )
        self.assertTrue(result.report.fits)
        self.assertEqual(result.report.future_reserve_target_tokens, 2000)
        self.assertEqual(result.report.future_reserve_tokens, 344)
        self.assertEqual(result.report.future_reserve_minimum_tokens, 300)
        self.assertEqual(result.report.sources[-1].action, "omitted")
        self.assertEqual(builder.visible_source_ids, {"S1"})
        for minimum in (-1, 2001):
            with self.subTest(minimum=minimum), self.assertRaisesRegex(ValueError, "minimum"):
                ContextBuilder(len, 5000).build(
                    [], instructions="", tools=[], text_format={}, request_number=1, focus="",
                    future_reserve_tokens=2000, minimum_future_reserve_tokens=minimum,
                )


class ContextAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_evidence_that_cannot_fit_stops_before_paid_request(self):
        def handler(request):
            if request.url.path.endswith("/labels"):
                return github_response(request)
            return httpx.Response(200, json={**ISSUE, "body": "Export failure. " * 300})

        responses = [simulated_response(content=final_content())]
        probe, _, _ = await run_simulated_agent(responses, handler=handler)
        budget = probe.context_builds[-1].estimated_tokens - 1
        result, create, emit = await run_simulated_agent(responses, settings=replace(SETTINGS, context_tokens=budget), handler=handler)
        self.assertIsNone(result.content)
        create.assert_not_awaited()
        self.assertEqual([source.kind for source in result.sources], ["issue", "repo_labels"])
        self.assertFalse(result.context_builds[-1].fits)
        self.assertFalse(result.context_builds[-1].sent)
        self.assertIn("not sent", result.limitations[-1])
        self.assertTrue(any("no model request was sent" in call.args[1] for call in emit.call_args_list))

    async def test_omitted_source_is_kept_for_review_but_cannot_support_model_claims(self):
        def handler(request):
            response = github_response(request)
            if request.url.path.endswith("/readme"):
                import base64
                return httpx.Response(200, json={**response.json(), "content": base64.b64encode(b"General project background. " * 100).decode()})
            return response

        prefix = [simulated_response([tool_call("get_repo_readme", call_id="readme")])]
        probe, _, _ = await run_simulated_agent([*prefix, simulated_response(content=final_content())], handler=handler)
        # Just too small for the full README with the final request's reserves, which include
        # room for one optional tool result, so its excerpt must be omitted.
        budget = probe.context_builds[-1].estimated_tokens_before + REPAIR_RESERVE_TOKENS + CANDIDATE_RESERVE_TOKENS - 30
        result, create, emit = await run_simulated_agent([
            *prefix, simulated_response(content=final_content("S3")), simulated_response(content=final_content()),
        ], settings=replace(SETTINGS, context_tokens=budget), handler=handler)
        self.assertIsNotNone(result.content)
        self.assertEqual(create.await_count, 3)
        self.assertEqual(result.sources[2].kind, "readme")
        self.assertTrue(result.sources[2].excerpt)
        self.assertEqual(json.loads(create.call_args_list[1].kwargs["input"][-1]["output"])["source"]["excerpt"], "")
        self.assertTrue(any("omitted excerpt cannot support" in call.args[1] for call in emit.call_args_list))
        self.assertTrue(any("Context Builder omitted" in item for item in result.limitations))
        self.assertTrue(all(report.fits and report.sent for report in result.context_builds))
        self.assertEqual(result.usage.model_requests, sum(report.sent for report in result.context_builds))
