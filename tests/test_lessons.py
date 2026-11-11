"""Offline checks for the lesson scripts. No credentials or network are used."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai.types.responses import ResponseFunctionToolCall, ResponseReasoningItem


LESSONS = Path(__file__).resolve().parent.parent / "lessons"


def response(output=(), status="completed", text="Offline test answer."):
    return SimpleNamespace(
        output=list(output), status=status, output_text=text,
        usage=SimpleNamespace(total_tokens=10),
    )


def run_lesson(filename, create):
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    output = StringIO()
    with (
        patch("dotenv.load_dotenv"),
        patch("openai.OpenAI", return_value=client),
        patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}),
        patch.object(sys, "argv", [filename]),
        redirect_stdout(output),
    ):
        runpy.run_path(str(LESSONS / filename), run_name="__main__")
    return output.getvalue()


class LessonTests(unittest.TestCase):
    def test_first_lesson_sends_sample_and_prints_answer(self):
        create = Mock(return_value=response())
        output = run_lesson("01_llm_call.py", create)
        self.assertEqual(create.call_count, 1)
        request = create.call_args.kwargs
        self.assertIn("Windows 11", request["input"])
        self.assertNotIn("tools", request)
        self.assertIn("Offline test answer.", output)

    def test_second_lesson_pairs_result_and_preserves_history(self):
        reasoning = ResponseReasoningItem(
            id="rs_test_123", type="reasoning", summary=[], encrypted_content="opaque-test-state",
        )
        call = ResponseFunctionToolCall(
            type="function_call", name="get_issue",
            arguments='{"issue_number":123}', call_id="call_test_123",
        )
        create = Mock(side_effect=[response([reasoning, call]), response()])
        output = run_lesson("02_tool_call.py", create)
        self.assertEqual(create.call_count, 2)
        first = create.call_args_list[0].kwargs
        second = create.call_args_list[1].kwargs
        self.assertNotIn("Windows 11", first["input"][0]["content"])
        self.assertEqual(first["tool_choice"], {"type": "function", "name": "get_issue"})
        self.assertEqual(second["input"][1], reasoning.to_dict())
        self.assertEqual(second["input"][2], call.to_dict())
        self.assertEqual(second["input"][1]["encrypted_content"], "opaque-test-state")
        result = second["input"][3]
        self.assertEqual(result["call_id"], call.call_id)
        self.assertEqual(result["type"], "function_call_output")
        self.assertEqual(json.loads(result["output"])["number"], 123)
        self.assertIn("Windows 11", json.loads(result["output"])["body"])
        self.assertEqual(second["tool_choice"], "none")
        self.assertIn("Offline test answer.", output)
        self.assertIn("Total tokens: 20", output)

    def test_unexpected_calls_stop_before_reading_the_fixture(self):
        cases = [("run_shell", '{"command":"whoami"}'), ("get_issue", '{"issue_number":124}')]
        for name, arguments in cases:
            with self.subTest(name=name):
                call = SimpleNamespace(type="function_call", name=name, arguments=arguments, call_id="test")
                create = Mock(return_value=response([call]))
                with patch("pathlib.Path.read_text") as read_file, self.assertRaises(ValueError):
                    run_lesson("02_tool_call.py", create)
                read_file.assert_not_called()
                self.assertEqual(create.call_count, 1)

    def test_incomplete_model_responses_stop_the_lesson(self):
        for filename in ("01_llm_call.py", "02_tool_call.py"):
            with self.subTest(filename=filename):
                create = Mock(return_value=response(status="incomplete"))
                with self.assertRaises(RuntimeError):
                    run_lesson(filename, create)
                self.assertEqual(create.call_count, 1)

    def test_removed_flags_cannot_trigger_api_requests(self):
        for filename in ("01_llm_call.py", "02_tool_call.py"):
            for flag in ("--dry-run", "--issue"):
                with self.subTest(filename=filename, flag=flag):
                    with (
                        patch("dotenv.load_dotenv") as load_env,
                        patch("openai.OpenAI") as constructor,
                        patch.object(sys, "argv", [filename, flag]),
                        self.assertRaises(SystemExit),
                    ):
                        runpy.run_path(str(LESSONS / filename), run_name="__main__")
                    load_env.assert_not_called()
                    constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
