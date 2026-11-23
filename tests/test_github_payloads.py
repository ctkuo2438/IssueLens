"""Offline checks that malformed GitHub payloads fail one lookup, not the whole analysis.

GitHub and model responses are simulated.
"""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.agent import AgentRun
from backend.app.github import GitHubTools, ToolError
from backend.app.jobs import JobManager
from test_mvp import ISSUE, SETTINGS, TARGET, OfflineEncoding, final_content, github_response, simulated_response


README = {
    "encoding": "base64", "size": 14, "path": "README.md", "content": "UHJvamVjdCBSRUFETUU=",
    "html_url": "https://github.com/example/project/blob/main/README.md",
}


class PayloadServer:
    """Serve one replaceable payload per endpoint and the default fixtures elsewhere."""

    def __init__(self) -> None:
        self.payloads: dict[str, object] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        for suffix, payload in self.payloads.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "bug", "description": "Something is broken", "color": "d73a4a"}])
        return github_response(request)


class GitHubPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = PayloadServer()
        self.client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(self.server))
        self.tools = GitHubTools(self.client, TARGET, lambda text, limit: (text, False))

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_malformed_issue_is_a_tool_error_without_evidence(self):
        cases = [
            [], "invalid", {}, {**ISSUE, "title": 42}, {**ISSUE, "state": None}, {**ISSUE, "body": 5},
            {**ISSUE, "user": "reporter"}, {**ISSUE, "user": {"id": 1}}, {**ISSUE, "labels": "bug"},
            {**ISSUE, "labels": [None]}, {**ISSUE, "labels": [{"color": "d73a4a"}]},
            {**ISSUE, "comments": "3"}, {**ISSUE, "comments": -1}, {**ISSUE, "comments": True},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.server.payloads["/issues/42"] = payload
                with self.assertRaisesRegex(ToolError, "invalid issue data"):
                    await self.tools.execute("get_issue", "{}")
                self.assertIsNone(self.tools.issue)
                self.assertEqual(self.tools.sources, {})

    async def test_documented_issue_shapes_are_accepted(self):
        # A nullable user and label names given as strings are both in GitHub's issue schema.
        self.server.payloads["/issues/42"] = {**ISSUE, "user": None, "labels": ["bug", {"name": "windows"}]}
        result = await self.tools.execute("get_issue", "{}")
        self.assertEqual(result["issue"]["author"], "(unknown)")
        self.assertEqual(result["issue"]["labels"], ["bug", "windows"])

    async def test_malformed_readme_is_a_tool_error_without_evidence(self):
        cases = [
            [], {}, {**README, "content": None}, {**README, "size": "14"},
            {**README, "html_url": 7}, {**README, "path": ["README.md"]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.server.payloads["/readme"] = payload
                with self.assertRaisesRegex(ToolError, "invalid README data"):
                    await self.tools.execute("get_repo_readme", "{}")
                self.assertEqual(self.tools.sources, {})
        self.server.payloads["/readme"] = README
        result = await self.tools.execute("get_repo_readme", "{}")
        self.assertIn("Project README", result["source"]["excerpt"])

    async def test_malformed_labels_are_a_tool_error_without_a_catalog(self):
        await self.tools.execute("get_issue", "{}")
        cases = [
            {"message": "unexpected"}, "invalid", [None], [{"color": "d73a4a"}],
            [{"name": 7}], [{"name": "bug", "description": 3}],
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.server.payloads["/labels"] = payload
                with self.assertRaisesRegex(ToolError, "invalid label data"):
                    await self.tools.execute("get_repo_labels", "{}")
                self.assertIsNone(self.tools.labels_source_id)
                self.assertEqual(self.tools.repo_labels, {})
                self.assertEqual(list(self.tools.sources), ["S1"])


class MalformedPayloadJobTests(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, server, responses):
        github = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(server))
        create = AsyncMock(side_effect=responses)
        client = AsyncMock()
        client.__aenter__.return_value.responses.create = create
        manager = JobManager(SETTINGS)
        with (
            patch("backend.app.agent.httpx.AsyncClient", return_value=github),
            patch("backend.app.agent.AsyncOpenAI", return_value=client),
            patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
            patch.object(AgentRun, "required_lookup", return_value=None),
        ):
            job = await manager.create(TARGET)
            assert job.task is not None
            await job.task
        return job

    async def test_malformed_labels_keep_the_answer(self):
        server = PayloadServer()
        server.payloads["/labels"] = {"message": "unexpected"}
        job = await self.run_job(server, [simulated_response(content=final_content())])
        result = job.snapshot.result
        assert result is not None
        self.assertIsNotNone(result.content)
        self.assertEqual([source.id for source in result.sources], ["S1"])
        self.assertIn("get_repo_labels: GitHub returned invalid label data.", result.limitations)
        self.assertEqual(job.trace.stop_reason, "final_answer")

    async def test_malformed_issue_reports_missing_evidence(self):
        server = PayloadServer()
        server.payloads["/issues/42"] = {**ISSUE, "user": "reporter"}
        job = await self.run_job(server, [simulated_response(content=final_content())])
        self.assertEqual(job.snapshot.status, "failed")
        self.assertEqual(job.snapshot.error, "No issue evidence could be retrieved. Check the URL and the activity log.")
        self.assertEqual(job.trace.stop_reason, "target_issue_unavailable")


if __name__ == "__main__":
    unittest.main()
