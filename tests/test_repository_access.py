"""Offline checks for the public-repository guard. GitHub and model responses are simulated."""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend.app.agent import AgentRun
from backend.app.checkpoint import AgentCheckpoint, PendingTools
from backend.app.github import GitHubTools, REPOSITORY_NOT_PUBLIC
from test_mvp import SETTINGS, TARGET, OfflineEncoding, final_content, github_response, simulated_response, tool_call


TOKEN_SETTINGS = replace(SETTINGS, github_token="offline-github-token")
REPOSITORY_PATH = "/repos/example/project"
PUBLIC = {"full_name": "example/project", "private": False, "visibility": "public"}
PRIVATE = {**PUBLIC, "private": True, "visibility": "private"}


async def run_agent(repository_response, settings=TOKEN_SETTINGS, checkpoint=None):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == REPOSITORY_PATH:
            return repository_response
        return github_response(request)

    github = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    create = AsyncMock(side_effect=[
        simulated_response([tool_call("get_issue")]), simulated_response(content=final_content()),
    ])
    client = AsyncMock()
    client.__aenter__.return_value.responses.create = create
    emit = Mock()
    run = AgentRun(
        TARGET, settings, emit, checkpoint=checkpoint,
        save_checkpoint=AsyncMock() if checkpoint is not None else None,
    )
    with (
        patch("backend.app.agent.httpx.AsyncClient", return_value=github),
        patch("backend.app.agent.AsyncOpenAI", return_value=client),
        patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
        patch.object(AgentRun, "required_lookup", return_value=None),
    ):
        result = await run.run()
    return run, result, create, paths, emit


def pending_issue_read() -> AgentCheckpoint:
    """A checkpoint saved after the model requested get_issue but before the read finished."""
    checkpoint = AgentCheckpoint.start("offline-signature")
    call = tool_call("get_issue")
    checkpoint.history = [
        {"role": "user", "content": f"Investigate this GitHub issue and draft a maintainer reply: {TARGET.url}"},
        call.to_dict(),
    ]
    checkpoint.pending_tools = PendingTools(
        calls=[call], required_action=None, require_comments=False, closing_for_repairs=False, at_limit=False,
    )
    return checkpoint


class PublicRepositoryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_repository_is_confirmed_once_before_any_issue_read(self):
        run, result, create, paths, _ = await run_agent(httpx.Response(200, json=PUBLIC))
        self.assertEqual(paths[0], REPOSITORY_PATH)
        self.assertEqual(paths.count(REPOSITORY_PATH), 1)
        self.assertIsNotNone(result.content)
        self.assertEqual(create.await_count, 2)
        # The server check is neither a model tool call nor a Trace attempt.
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual([attempt.kind for attempt in run.trace.attempts], ["model", "tool", "model"])

    async def test_private_and_missing_repositories_share_one_outcome_without_reads(self):
        cases = {
            "private": httpx.Response(200, json=PRIVATE),
            "internal": httpx.Response(200, json={**PUBLIC, "private": True, "visibility": "internal"}),
            "unknown visibility": httpx.Response(200, json={"full_name": "example/project"}),
            "missing": httpx.Response(404, json={"message": "Not Found"}),
        }
        outcomes = set()
        for name, response in cases.items():
            with self.subTest(name):
                run, result, create, paths, emit = await run_agent(response)
                self.assertEqual(paths, [REPOSITORY_PATH])
                self.assertEqual(create.await_count, 0)
                self.assertIsNone(result.content)
                self.assertEqual(result.sources, [])
                self.assertIn(REPOSITORY_NOT_PUBLIC, result.limitations)
                self.assertEqual(run.trace.stop_reason, "repository_not_public")
                outcomes.add((tuple(result.limitations), repr(emit.call_args_list)))
        # A single public outcome means a server credential cannot reveal which private repositories exist.
        self.assertEqual(len(outcomes), 1)

    async def test_failed_visibility_check_fails_closed_without_model_cost(self):
        run, result, create, paths, _ = await run_agent(httpx.Response(503))
        self.assertEqual(paths, [REPOSITORY_PATH])
        self.assertEqual(create.await_count, 0)
        self.assertEqual(result.sources, [])
        self.assertEqual(run.trace.stop_reason, "repository_check_failed")
        self.assertIn(
            "Could not confirm that the repository is public; no issue data was read. GitHub returned temporary HTTP 503.",
            result.limitations,
        )

    async def test_anonymous_runs_skip_the_check_because_they_cannot_read_private_data(self):
        _, result, _, paths, _ = await run_agent(httpx.Response(200, json=PRIVATE), settings=SETTINGS)
        self.assertNotIn(REPOSITORY_PATH, paths)
        self.assertIsNotNone(result.content)

    async def test_resumed_tool_round_is_checked_before_pending_reads(self):
        _, result, create, paths, _ = await run_agent(httpx.Response(200, json=PRIVATE), checkpoint=pending_issue_read())
        self.assertEqual(paths, [REPOSITORY_PATH])
        self.assertEqual(create.await_count, 0)
        self.assertEqual(result.sources, [])

    async def test_confirmed_repository_is_not_rechecked_after_resume(self):
        # Skipping the repeat check also keeps a saved GitHub cooldown or retry wait
        # from turning a confirmed repository into a failed check.
        checkpoint = pending_issue_read()
        async with httpx.AsyncClient() as unused:
            tools = GitHubTools(unused, TARGET, lambda text, limit: (text, False))
            tools.repository_public = True
            checkpoint.tools = tools.checkpoint()
        _, result, _, paths, _ = await run_agent(httpx.Response(200, json=PRIVATE), checkpoint=checkpoint)
        self.assertNotIn(REPOSITORY_PATH, paths)
        self.assertIn(f"{REPOSITORY_PATH}/issues/42", paths)
        self.assertIsNotNone(result.content)


if __name__ == "__main__":
    unittest.main()
