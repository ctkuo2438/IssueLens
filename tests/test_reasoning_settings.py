"""Offline reasoning configuration checks, including the real SDK serializer."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from backend.app import config
from backend.app.agent import AgentRun, MAX_OUTPUT_TOKENS
from backend.app.config import REASONING_EFFORTS, Settings
from test_mvp import (
    OfflineEncoding, SETTINGS, TARGET, final_content, github_response,
    run_simulated_agent, simulated_response, tool_call,
)
from test_required_lookups import PolicyServer, content_with_comparison, prefix, run_policy, step


class ReasoningSettingsTests(unittest.TestCase):
    def settings(self, values):
        with patch.dict(os.environ, values, clear=True), patch.object(config, "load_dotenv"):
            return Settings.from_env()

    def test_unset_or_blank_effort_keeps_provider_default_without_changing_budgets(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                settings = self.settings({} if value is None else {"OPENAI_REASONING_EFFORT": value})
                self.assertIsNone(settings.reasoning_effort)
                self.assertEqual(settings.model, "gpt-5.6")
                self.assertEqual((settings.max_iterations, settings.max_tool_calls, settings.runtime_seconds, settings.context_tokens), (8, 15, 90, 12000))

    def test_supported_values_are_loaded_with_explicit_none_distinct_from_unset(self):
        for effort in ("none", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                settings = self.settings({"OPENAI_MODEL": "gpt-5.6", "OPENAI_REASONING_EFFORT": f" {effort} "})
                self.assertEqual(settings.model, "gpt-5.6")
                self.assertEqual(settings.reasoning_effort, effort)
        self.assertNotIn("minimal", REASONING_EFFORTS)

    def test_invalid_effort_fails_without_echoing_the_supplied_value(self):
        for value in ("extreme", "minimal", "Medium", "SENSITIVE_INPUT_CANARY"):
            with self.subTest(value=value), self.assertRaises(ValueError) as failure:
                self.settings({"OPENAI_REASONING_EFFORT": value})
            self.assertIn("OPENAI_REASONING_EFFORT", str(failure.exception))
            self.assertNotIn(value, str(failure.exception))

    def test_actual_dotenv_file_loads_model_and_effort(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("OPENAI_MODEL=gpt-5.6\nOPENAI_REASONING_EFFORT=medium\n")
            with patch.object(config, "ROOT", root), patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.model, "gpt-5.6")
            self.assertEqual(settings.reasoning_effort, "medium")

    def test_exported_effort_keeps_existing_dotenv_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("OPENAI_REASONING_EFFORT=medium\n")
            with patch.object(config, "ROOT", root), patch.dict(os.environ, {"OPENAI_REASONING_EFFORT": "low"}, clear=True):
                self.assertEqual(Settings.from_env().reasoning_effort, "low")


class ReasoningAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_effort_is_fixed_across_tool_planning_and_same_evidence_repair(self):
        settings = replace(SETTINGS, model="gpt-5.6", reasoning_effort="medium")
        result, create, emit = await run_simulated_agent([
            simulated_response([tool_call("get_issue")]),
            simulated_response(content=final_content("S999")),
            simulated_response(content=final_content()),
        ], settings=settings)
        self.assertIsNotNone(result.content)
        self.assertEqual(len(result.rejected_answers), 1)
        for call in create.call_args_list:
            self.assertEqual(call.kwargs["model"], "gpt-5.6")
            self.assertEqual(call.kwargs["reasoning"], {"effort": "medium"})
            self.assertEqual(call.kwargs["max_output_tokens"], 2400)
            self.assertFalse(call.kwargs["store"])
        self.assertEqual(create.call_args_list[1].kwargs["input"], create.call_args_list[2].kwargs["input"])
        self.assertTrue(any("reasoning effort: medium" in call.args[1] for call in emit.call_args_list))

    async def test_real_sdk_wire_body_omits_unset_effort_and_preserves_explicit_none(self):
        for effort in (None, "none", "medium"):
            with self.subTest(effort=effort):
                requests = []

                def respond(request):
                    requests.append(json.loads(request.content))
                    output = [tool_call("get_issue").to_dict()] if len(requests) == 1 else [{
                        "id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(final_content()), "annotations": []}],
                    }]
                    return httpx.Response(200, json={
                        "id": f"resp_offline_{len(requests)}", "object": "response", "created_at": 0,
                        "model": "gpt-5.6", "status": "completed", "output": output,
                        "incomplete_details": None,
                        "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
                    })

                # Both transports are local fixtures. The SDK still serializes and
                # parses the actual wire objects, without making network requests.
                model_http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                model = AsyncOpenAI(api_key="offline-key", http_client=model_http)
                github = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(github_response))
                try:
                    with (
                        patch("backend.app.agent.AsyncOpenAI", return_value=model),
                        patch("backend.app.agent.httpx", SimpleNamespace(AsyncClient=lambda **_: github)),
                        patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
                        patch.object(AgentRun, "required_lookup", return_value=None),
                    ):
                        result = await AgentRun(TARGET, replace(SETTINGS, model="gpt-5.6", reasoning_effort=effort), lambda *_: None).run()
                finally:
                    await model.close()
                    await github.aclose()
                self.assertIsNotNone(result.content)
                self.assertEqual(len(requests), 2)
                for request in requests:
                    if effort is None:
                        self.assertNotIn("reasoning", request)
                    else:
                        self.assertEqual(request["reasoning"], {"effort": effort})
                    self.assertEqual(request["max_output_tokens"], MAX_OUTPUT_TOKENS)
                    self.assertNotIn("offline-key", json.dumps(request))

    async def test_output_budget_exhaustion_is_explicit_and_preserves_sources(self):
        result, create, _ = await run_simulated_agent([
            simulated_response([tool_call("get_issue")]),
            simulated_response([tool_call("get_repo_readme")], status="incomplete", incomplete_reason="max_output_tokens"),
        ], settings=replace(SETTINGS, model="gpt-5.6", reasoning_effort="medium"))
        self.assertEqual(create.await_count, 2)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertIsNone(result.content)
        self.assertTrue(result.sources)
        self.assertTrue(any("max_output_tokens reached 2400" in item for item in result.limitations))
        self.assertTrue(any("reasoning and visible text" in item for item in result.limitations))

    async def test_missing_incomplete_details_does_not_guess_budget_exhaustion(self):
        result, _, _ = await run_simulated_agent([
            simulated_response([tool_call("get_issue")]),
            simulated_response(status="incomplete"),
        ], settings=replace(SETTINGS, reasoning_effort="medium"))
        self.assertIsNone(result.content)
        self.assertTrue(any("incomplete" in item for item in result.limitations))
        self.assertFalse(any("max_output_tokens reached" in item for item in result.limitations))

    async def test_full_minimum_lookup_policy_retains_explicit_effort(self):
        result, create, _ = await run_policy([
            *prefix(), step("get_related_issue", issue_number=41),
            simulated_response(content=content_with_comparison()),
        ], PolicyServer(), replace(SETTINGS, model="gpt-5.6", reasoning_effort="medium"))
        self.assertIsNotNone(result.content)
        self.assertEqual(result.similar_issue_check.compared_candidate_numbers, [41])
        self.assertTrue(all(call.kwargs["reasoning"] == {"effort": "medium"} for call in create.call_args_list))


if __name__ == "__main__":
    unittest.main()
