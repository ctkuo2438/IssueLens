"""How each GitHub response status becomes a response, a refusal, or a retry with a cooldown."""

import time
import unittest

import httpx

from backend.app.github import GitHubResponseError, GitHubTools, TransientToolError
from backend.app.retry import MIN_RETRY_DELAY
from test_mvp import TARGET


async def fetch(response: httpx.Response, *, allow_not_found: bool = False) -> tuple[GitHubTools, object]:
    """One GitHub read answered with response: the tools afterwards, and the response or the error."""
    transport = httpx.MockTransport(lambda request: response)
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=transport) as client:
        tools = GitHubTools(client, TARGET, lambda text, limit: (text, False))
        try:
            return tools, await tools._get(TARGET.api_path, allow_not_found=allow_not_found)
        except Exception as exc:
            return tools, exc


class GitHubStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_an_allowed_missing_resource_return_the_response(self):
        for status, allow_not_found in ((200, False), (404, True)):
            with self.subTest(status=status):
                tools, outcome = await fetch(httpx.Response(status, json={}), allow_not_found=allow_not_found)
                assert isinstance(outcome, httpx.Response)
                self.assertEqual(outcome.status_code, status)
                self.assertIsNone(tools.cooldown_until)

    async def test_refusals_report_their_status_without_a_cooldown(self):
        for status, message in (
            (401, "rejected GITHUB_TOKEN"), (403, "denied access"), (404, "could not find"),
            (302, "redirects are not followed"),
        ):
            with self.subTest(status=status):
                tools, outcome = await fetch(httpx.Response(status, json={"message": "Refused."}))
                assert isinstance(outcome, GitHubResponseError)
                self.assertEqual(outcome.status_code, status)
                self.assertIn(message, str(outcome))
                self.assertIsNone(tools.cooldown_until)

    async def test_a_requested_wait_becomes_a_retry_and_a_cooldown(self):
        for response in (
            httpx.Response(403, headers={"retry-after": "30"}, json={"message": "You have exceeded a secondary rate limit."}),
            httpx.Response(429, headers={"retry-after": "30"}, json={}),
            httpx.Response(503, headers={"retry-after": "30"}, json={}),
        ):
            with self.subTest(status=response.status_code):
                before = time.time()
                tools, outcome = await fetch(response)
                assert isinstance(outcome, TransientToolError)
                self.assertEqual(outcome.failure.delay, 30)
                assert tools.cooldown_until is not None
                self.assertGreaterEqual(tools.cooldown_until, before + 30)

    async def test_a_temporary_status_without_a_requested_wait_retries_without_a_cooldown(self):
        tools, outcome = await fetch(httpx.Response(503, json={}))
        assert isinstance(outcome, TransientToolError)
        self.assertEqual(outcome.failure.delay, MIN_RETRY_DELAY)
        self.assertIsNone(tools.cooldown_until)
