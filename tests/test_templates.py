"""Offline checks using fictional templates and simulated GitHub/model responses."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

from backend.app.github import GitHubTools, ToolError
from backend.app.main import create_app
from backend.app.templates import parse_template
from test_mvp import ISSUE, SETTINGS, TARGET, final_content, run_simulated_agent, simulated_response, tool_call


FIXTURES = Path(__file__).parent / "fixtures"
FORM = (FIXTURES / "bug-report.yml").read_text()
MARKDOWN = (FIXTURES / "feature-request.md").read_text()
DIRECTORY = ".github/ISSUE_TEMPLATE"


class OfflineTemplateEncoding:
    """Four-character test units for integration, not model tokens or billing estimates."""

    def encode(self, text, disallowed_special=()):
        return [text[index:index + 4] for index in range(0, len(text), 4)]

    def decode(self, tokens):
        return "".join(tokens)


class TemplateServer:
    def __init__(self):
        self.files = {"bug-report.yml": FORM, "feature-request.md": MARKDOWN, "config.yml": "blank_issues_enabled: false"}
        self.requests = []
        self.directory_status = 200
        self.entries = None
        self.overrides = {}

    def metadata(self, name):
        raw = self.files[name].encode()
        return {
            "name": name, "path": f"{DIRECTORY}/{name}", "type": "file", "size": len(raw),
            "sha": hashlib.sha256(raw).hexdigest(),
            "html_url": f"https://github.com/example/project/blob/main/{DIRECTORY}/{name}",
        }

    def respond(self, request):
        self.requests.append(request)
        if request.url.path == f"{TARGET.api_path}/contents/{DIRECTORY}":
            if self.directory_status != 200:
                return httpx.Response(self.directory_status, json={"message": "A simulated directory response."})
            return httpx.Response(200, json=self.entries if self.entries is not None else [self.metadata(name) for name in self.files])
        for name, text in self.files.items():
            if request.url.path == f"{TARGET.api_path}/contents/{DIRECTORY}/{name}":
                data = {**self.metadata(name), "encoding": "base64", "content": base64.b64encode(text.encode()).decode(), **self.overrides.get(name, {})}
                return httpx.Response(200, json=data)
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "bug", "description": "Reports a malfunction.", "color": "d73a4a"}])
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})
        return httpx.Response(200, json=ISSUE)


class TemplateParserTests(unittest.TestCase):
    def test_form_distinguishes_required_optional_defaults_and_checkbox_options(self):
        template = parse_template(f"{DIRECTORY}/bug.yml", FORM, "S3")
        self.assertTrue(template.requirements_known)
        self.assertEqual(template.name, "Bug report")
        fields = {field.label: field for field in template.fields}
        self.assertTrue(fields["Application version"].required)
        self.assertTrue(fields["Reproduction steps"].required)
        self.assertFalse(fields["Additional logs"].required)
        self.assertFalse(fields["Contact details"].required)
        self.assertFalse(fields["Reporting checklist"].required)
        self.assertEqual(fields["Reporting checklist"].required_options, ["I checked existing issues."])

    def test_markdown_headings_do_not_become_required_fields(self):
        template = parse_template(f"{DIRECTORY}/feature.md", MARKDOWN, "S4")
        self.assertEqual(template.name, "Feature request")
        self.assertEqual(template.description, "Suggest a new capability.")
        self.assertFalse(template.requirements_known)
        self.assertEqual(template.fields, [])
        self.assertEqual(template.notes, [])

    def test_invalid_flags_and_unknown_input_types_remain_unknown(self):
        for change in ('required: "true"', 'required: 1'):
            with self.subTest(change=change):
                template = parse_template("bug.yml", FORM.replace("required: true", change), "S1")
                self.assertFalse(template.requirements_known)
                self.assertIsNone(template.fields[0].required)
        template = parse_template("bug.yml", FORM.replace("type: input", "type: [unexpected]"), "S1")
        self.assertFalse(template.requirements_known)
        self.assertTrue(template.notes)

    def test_unsafe_or_malformed_yaml_is_retained_as_unknown_without_construction(self):
        for text in ("name: [", "body: &anchor [*anchor]", "!!python/object/apply:os.system ['unexpected']", "body: " + "[" * 40 + "]" * 40):
            with self.subTest(text=text):
                template = parse_template("bug.yml", text, "S1")
                self.assertFalse(template.requirements_known)
                self.assertTrue(template.notes)
                self.assertEqual(template.fields, [])

    def test_large_field_sets_are_explicitly_partial(self):
        text = "name: Many fields\nbody:\n" + "".join(f"  - type: input\n    attributes:\n      label: Field {index}\n" for index in range(13))
        template = parse_template("bug.yml", text, "S1")
        self.assertEqual(len(template.fields), 12)
        self.assertFalse(template.requirements_known)
        self.assertTrue(any("twelve" in note for note in template.notes))

    def test_unusable_markdown_front_matter_keeps_the_file_name(self):
        for text, notes in (
            ("# Heading", []),
            ("---\nname: Never closed\n# Heading", []),
            ("---\n- a list\n---\n# Heading", []),
            ("---\nname: [\n---\n# Heading", ["Markdown front matter could not be parsed safely."]),
        ):
            with self.subTest(text=text):
                template = parse_template(f"{DIRECTORY}/feature.md", text, "S1")
                self.assertEqual((template.name, template.description), ("feature.md", ""))
                self.assertEqual(template.notes, notes)

    def test_a_form_without_a_body_list_has_unknown_requirements(self):
        for text in ("name: No body", "name: Text body\nbody: text"):
            with self.subTest(text=text):
                template = parse_template("bug.yml", text, "S1")
                self.assertEqual(template.name, "bug.yml")
                self.assertEqual(template.fields, [])
                self.assertEqual(template.notes, ["The YAML file does not contain a supported form body."])
                self.assertFalse(template.requirements_known)

    def test_unusable_form_elements_are_omitted_with_notes(self):
        text = (
            "name: Elements\nbody:\n"
            "  - just text\n"
            "  - type: markdown\n    attributes:\n      value: Introduction\n"
            "  - type: input\n    attributes:\n      description: No label\n"
            "  - type: input\n    attributes:\n      label: Version\n"
        )
        template = parse_template("bug.yml", text, "S1")
        self.assertEqual([field.label for field in template.fields], ["Version"])
        self.assertEqual(template.notes, [
            "An unsupported form element was omitted.", "An input without a supported label was omitted.",
        ])
        self.assertFalse(template.requirements_known)

    def test_checkbox_requirements_are_bounded_or_unknown(self):
        def checkboxes(options: str) -> str:
            return f"name: Checks\nbody:\n  - type: checkboxes\n    attributes:\n      label: Checks\n      options:{options}\n"

        many = "".join(f"\n        - label: Option {index}\n          required: true" for index in range(7))
        template = parse_template("bug.yml", checkboxes(many), "S1")
        self.assertEqual(template.fields[0].required_options, [f"Option {index}" for index in range(6)])
        self.assertEqual(template.notes, ["Only six required checkbox options were extracted."])
        for options in (" none", "\n        - label: ''\n          required: true"):
            with self.subTest(options=options):
                template = parse_template("bug.yml", checkboxes(options), "S1")
                self.assertIsNone(template.fields[0].required)
                self.assertEqual(template.notes, ["Some requirement flags were not valid booleans; their status is unknown."])

    def test_long_text_is_shortened_with_one_note(self):
        text = f"name: {'N' * 130}\nbody:\n  - type: input\n    attributes:\n      label: {'L' * 130}\n"
        template = parse_template("bug.yml", text, "S1")
        self.assertEqual((len(template.name), len(template.fields[0].label)), (120, 120))
        self.assertEqual(template.notes, ["Some extracted text was shortened; review the original template."])


class TemplateToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = TemplateServer()
        self.client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(self.server.respond))
        self.tools = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))
        await self.tools.execute("get_issue", "{}")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_reads_only_scoped_supported_files_with_sources_and_caching(self):
        result = await self.tools.execute("get_issue_templates", "{}")
        await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(len(self.server.requests), 4)
        self.assertTrue(all(request.method == "GET" for request in self.server.requests))
        self.assertEqual(len(result["templates"]), 2)
        self.assertEqual(result["templates"][0]["source_id"], "S3")
        self.assertEqual(result["templates"][1]["source_id"], "S4")
        self.assertEqual(self.tools.templates_source_id, "S2")
        self.assertTrue(self.tools.templates_complete)
        self.assertFalse(any(request.url.path.endswith("config.yml") for request in self.server.requests))
        self.assertEqual(len(self.tools.sources), 4)

    async def test_requires_target_and_rejects_model_supplied_scope_before_network(self):
        fresh = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))
        with self.assertRaises(ToolError):
            await fresh.execute("get_issue_templates", "{}")
        for args in ('{"owner":"other"}', '{"path":"../../secret"}'):
            with self.assertRaises(ValidationError):
                await self.tools.execute("get_issue_templates", args)
        self.assertEqual(len(self.server.requests), 1)

    async def test_missing_directory_is_successfully_checked_and_cached(self):
        self.server.directory_status = 404
        result = await self.tools.execute("get_issue_templates", "{}")
        await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(result["templates"], [])
        self.assertTrue(result["complete"])
        self.assertFalse(json.loads(result["lookup"]["excerpt"])["directory_found"])
        self.assertIsNotNone(self.tools.templates_source_id)
        self.assertEqual(len(self.server.requests), 2)

    async def test_directory_failure_is_not_an_empty_success(self):
        self.server.directory_status = 429
        with self.assertRaises(ToolError):
            await self.tools.execute("get_issue_templates", "{}")
        self.assertIsNone(self.tools.templates_source_id)
        self.assertIsNone(self.tools.templates_complete)

    async def test_config_only_directory_has_no_submission_templates(self):
        self.server.files = {"config.yml": "blank_issues_enabled: false"}
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(result["templates"], [])
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.server.requests), 2)

    async def test_unexpected_listing_and_file_paths_are_rejected(self):
        self.server.entries = [
            {"name": "../escape.yml", "path": "elsewhere/escape.yml", "type": "file"},
            {"name": "linked.yml", "path": f"{DIRECTORY}/linked.yml", "type": "symlink"},
        ]
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(result["templates"], [])
        self.assertFalse(result["complete"])
        self.assertEqual(len(self.server.requests), 2)

    async def test_file_identity_encoding_and_source_errors_are_partial(self):
        for override in (
            {"sha": "changed"}, {"path": "other/file.yml"}, {"type": "symlink"},
            {"html_url": "https://example.invalid/template.yml"}, {"content": "not base64"},
            {"encoding": "none"}, {"content": base64.b64encode(b"\xff").decode()},
        ):
            with self.subTest(override=override):
                self.server.overrides = {"bug-report.yml": override}
                fresh = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))
                await fresh.execute("get_issue", "{}")
                result = await fresh.execute("get_issue_templates", "{}")
                self.assertFalse(result["complete"])
                self.assertEqual([item["format"] for item in result["templates"]], ["markdown"])

    async def test_file_count_and_size_limits_are_explicit(self):
        self.server.files["third.yml"] = FORM
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(len(result["templates"]), 2)
        self.assertFalse(result["complete"])
        self.assertEqual(len(self.server.requests), 4)
        self.server.files = {"huge.yml": "x" * 32_769}
        fresh = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))
        await fresh.execute("get_issue", "{}")
        count = len(self.server.requests)
        result = await fresh.execute("get_issue_templates", "{}")
        self.assertEqual(result["templates"], [])
        self.assertFalse(result["complete"])
        self.assertEqual(len(self.server.requests) - count, 1)

    async def test_shared_context_limit_does_not_create_a_fake_empty_catalog(self):
        self.tools.clip = lambda text, limit: (text, limit == 2000)
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(result["templates"], [])
        self.assertFalse(result["complete"])
        self.assertIsNotNone(self.tools.templates_source_id)
        self.assertEqual(len(self.tools.sources), 2)
        self.assertTrue(self.tools.limitations)

    async def test_malformed_form_still_has_a_source_and_unknown_requirements(self):
        self.server.files = {"bug.yml": "body: ["}
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(len(result["templates"]), 1)
        self.assertFalse(result["templates"][0]["requirements_known"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["sources"][0]["excerpt"], "body: [")

    async def test_a_listing_that_is_not_a_list_is_rejected(self):
        self.server.entries = {"message": "Not a directory."}
        with self.assertRaisesRegex(ToolError, "unexpected issue template directory"):
            await self.tools.execute("get_issue_templates", "{}")
        self.assertIsNone(self.tools.templates_source_id)

    async def test_an_entry_that_is_not_an_object_makes_the_lookup_partial(self):
        self.server.entries = ["not an entry", self.server.metadata("bug-report.yml")]
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual([item["format"] for item in result["templates"]], ["form"])
        self.assertFalse(result["complete"])

    async def test_a_listing_of_a_thousand_entries_may_be_truncated(self):
        self.server.entries = [self.server.metadata("bug-report.yml")] + [
            {"name": f"notes-{index}.txt", "path": f"{DIRECTORY}/notes-{index}.txt", "type": "file"} for index in range(999)
        ]
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(len(result["templates"]), 1)
        self.assertFalse(result["complete"])
        self.assertIn("may be limited to 1,000 entries", result["lookup"]["excerpt"])

    async def test_content_larger_than_its_listed_size_is_rejected(self):
        self.server.files = {"huge.yml": "x" * 32_769}
        self.server.entries = [{**self.server.metadata("huge.yml"), "size": 100}]
        result = await self.tools.execute("get_issue_templates", "{}")
        self.assertEqual(result["templates"], [])
        self.assertFalse(result["complete"])
        self.assertIn("huge.yml: The template exceeds the 32 KiB file limit.", result["lookup"]["excerpt"])


class TemplateIntegrationTests(unittest.TestCase):
    def test_agent_job_and_sse_preserve_template_requirements_and_citations(self):
        server = TemplateServer()
        content = final_content()
        content["known_facts"].append({"text": "The current bug form requires reproduction steps.", "source_ids": ["S4"]})
        result, _, _ = asyncio.run(run_simulated_agent([
            simulated_response([tool_call("get_issue_templates")]),
            simulated_response([tool_call("search_issues", '{"query":"export"}', "call_search")]),
            simulated_response(content=content),
        ], handler=server.respond, encoding=OfflineTemplateEncoding()))
        self.assertIsNotNone(result.content)
        self.assertEqual(result.templates_source_id, "S3")
        self.assertEqual(len(result.issue_templates), 2)
        self.assertEqual(result.limitations, [])
        with patch("backend.app.jobs.AgentRun.run", new=AsyncMock(return_value=result)):
            with TestClient(create_app(SETTINGS)) as client:
                job_id = client.post("/api/v1/analyses", json={"issue_url": TARGET.url}).json()["job_id"]
                stream = client.get(f"/api/v1/analyses/{job_id}/events")
                self.assertIn("event: done", stream.text)
                snapshot = client.get(f"/api/v1/analyses/{job_id}").json()
        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(snapshot["result"]["issue_templates"][0]["fields"][0]["required"])
        self.assertFalse(snapshot["result"]["issue_templates"][1]["requirements_known"])


if __name__ == "__main__":
    unittest.main()
