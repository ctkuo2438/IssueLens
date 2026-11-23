"""Offline checks for the public-repository guard. GitHub and model responses are simulated."""

from contextlib import nullcontext
from dataclasses import dataclass, replace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend.app.agent import AgentRun
from backend.app.checkpoint import AgentCheckpoint, PendingTools
from backend.app.github import (
    GitHubTools, REPOSITORY_CHECK_FAILED, REPOSITORY_NOT_PUBLIC, RepositoryNotPublicError,
)
from backend.app.jobs import JobManager
from backend.app.models import AnalysisResult, RejectedAnswer, Source
from test_mvp import SETTINGS, TARGET, OfflineEncoding, final_content, github_response, simulated_response, tool_call


TOKEN_SETTINGS = replace(SETTINGS, github_token="offline-github-token")
REPOSITORY_PATH = "/repos/example/project"
ISSUE_PATH = f"{REPOSITORY_PATH}/issues/42"
PUBLIC = {"full_name": "example/project", "private": False, "visibility": "public"}
PRIVATE = {**PUBLIC, "private": True, "visibility": "private"}


def answer(status=200, body=PUBLIC, headers=None):
    """Build a fresh simulated GitHub repository response for each request."""
    def build():
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)
    return build


@dataclass
class Outcome:
    run: AgentRun
    result: AnalysisResult
    create: AsyncMock
    paths: list[str]
    emit: Mock
    saved: AsyncMock
    sleep: AsyncMock | None
    # Each saved checkpoint as JSON at the moment it was saved. A saved object can share
    # state, such as usage, that the run changes afterwards.
    saved_json: list[str]


def offline_patches(github, client):
    return (
        patch("backend.app.agent.httpx.AsyncClient", return_value=github),
        patch("backend.app.agent.AsyncOpenAI", return_value=client),
        patch("backend.app.agent.tiktoken.get_encoding", return_value=OfflineEncoding()),
        patch.object(AgentRun, "required_lookup", return_value=None),
    )


def simulated_services(answers, paths, responses=None):
    queue = list(answers) or [answer()]

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == REPOSITORY_PATH:
            return (queue.pop(0) if len(queue) > 1 else queue[0])()
        return github_response(request)

    github = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    create = AsyncMock(side_effect=responses or [simulated_response(content=final_content())])
    client = AsyncMock()
    client.__aenter__.return_value.responses.create = create
    return github, client, create


async def run_agent(*answers, settings=TOKEN_SETTINGS, checkpoint=None, fake_sleep=False, responses=None):
    # Only use fake_sleep without a GitHub cooldown: a cooldown wait needs real time to pass.
    paths = []
    github, client, create = simulated_services(answers, paths, responses)
    emit = Mock()
    saved_json: list[str] = []
    saved = AsyncMock(side_effect=lambda state: saved_json.append(state.model_dump_json()))
    run = AgentRun(TARGET, settings, emit, checkpoint=checkpoint, save_checkpoint=saved if checkpoint else None)
    gateway, model, encoding, lookups = offline_patches(github, client)
    sleeping = patch("backend.app.agent.asyncio.sleep", new_callable=AsyncMock) if fake_sleep else nullcontext()
    with gateway, model, encoding, lookups, sleeping as sleep:
        result = await run.run()
    return Outcome(run, result, create, paths, emit, saved, sleep, saved_json)


def resumed_checkpoint(*, repository_public=False, cooldown_until=None, restored_evidence=False) -> AgentCheckpoint:
    """A checkpoint saved during the server's get_issue read, before the read finished."""
    checkpoint = AgentCheckpoint.start("offline-signature")
    call = tool_call("get_issue", call_id="server_get_issue")
    checkpoint.history = [
        {"role": "user", "content": f"Investigate this GitHub issue and draft a maintainer reply: {TARGET.url}"},
        call.to_dict(),
    ]
    checkpoint.pending_tools = PendingTools(
        calls=[call], required_action=None, closing_for_repairs=False, at_limit=False,
    )
    tools = GitHubTools(Mock(spec=httpx.AsyncClient), TARGET, lambda text, limit: (text, False))
    tools.repository_public = repository_public
    tools.cooldown_until = cooldown_until
    if restored_evidence:
        # For example, an older checkpoint that read a repository before this check existed.
        source = Source(id="S1", kind="issue", title="Restored", url=TARGET.url, excerpt="RESTORED_EVIDENCE_CANARY")
        tools.sources = {"S1": source}
        tools.limitations = ["RESTORED_LIMITATION_CANARY"]
        checkpoint.result.sources = [source]
        checkpoint.result.rejected_answers = [RejectedAnswer(
            request_number=1, reason="offline", output_text="RESTORED_OUTPUT_CANARY", output_sha256="0" * 64, feedback="",
        )]
        checkpoint.result.usage.model_requests = 1
    checkpoint.tools = tools.checkpoint()
    return checkpoint


class PublicRepositoryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_repository_is_confirmed_once_before_any_issue_read(self):
        outcome = await run_agent(answer(), checkpoint=AgentCheckpoint.start("offline-signature"))
        self.assertEqual(outcome.paths[0], REPOSITORY_PATH)
        self.assertEqual(outcome.paths.count(REPOSITORY_PATH), 1)
        self.assertIsNotNone(outcome.result.content)
        self.assertEqual(outcome.create.await_count, 1)
        # The visibility check is neither a tool call nor a Trace attempt; the issue and catalog reads are both.
        self.assertEqual(outcome.result.usage.tool_calls, 2)
        self.assertEqual([attempt.kind for attempt in outcome.run.trace.attempts], ["tool", "tool", "model"])
        # The confirmation is saved with the run, so a replacement Worker does not repeat it.
        first_save = outcome.saved.await_args_list[0].args[0]
        self.assertTrue(first_save.tools.repository_public)

    async def test_every_repository_dependent_refusal_shares_one_outcome_without_reads(self):
        cases = {
            "private": answer(body=PRIVATE),
            "internal": answer(body={**PUBLIC, "private": True, "visibility": "internal"}),
            "no visibility": answer(body={"full_name": "example/project", "private": False}),
            "no fields": answer(body={"full_name": "example/project"}),
            "malformed": answer(body=b"not json"),
            "missing": answer(404, {"message": "Not Found"}),
            "renamed": answer(301, {"message": "Moved Permanently"}),
            "refused": answer(403, {"message": "Resource protected by organization SAML enforcement."}),
        }
        outcomes = set()
        for name, response in cases.items():
            with self.subTest(name):
                outcome = await run_agent(response)
                self.assertEqual(outcome.paths, [REPOSITORY_PATH])
                self.assertEqual(outcome.create.await_count, 0)
                self.assertIsNone(outcome.result.content)
                self.assertEqual(outcome.result.sources, [])
                self.assertEqual(outcome.result.limitations, [REPOSITORY_NOT_PUBLIC])
                self.assertEqual(outcome.run.trace.stop_reason, "repository_not_public")
                outcomes.add(repr(outcome.emit.call_args_list))
        # One public outcome means a server credential cannot reveal which private repositories exist.
        self.assertEqual(len(outcomes), 1)

    async def test_rejected_server_token_is_a_check_failure_for_every_repository(self):
        outcome = await run_agent(answer(401, {"message": "Bad credentials"}))
        self.assertEqual(outcome.create.await_count, 0)
        self.assertEqual(outcome.run.trace.stop_reason, "repository_check_failed")
        self.assertEqual(outcome.result.limitations, [
            "Could not confirm that the repository is public, so no repository data is included. "
            "GitHub rejected GITHUB_TOKEN. Check the server configuration.",
        ])

    async def test_temporary_failure_is_retried_once(self):
        outcome = await run_agent(answer(503, {}), answer(), fake_sleep=True)
        self.assertEqual(outcome.paths.count(REPOSITORY_PATH), 2)
        assert outcome.sleep is not None
        outcome.sleep.assert_awaited_once_with(1.0)
        self.assertIsNotNone(outcome.result.content)

    async def test_repeated_temporary_failure_fails_closed_without_model_cost(self):
        outcome = await run_agent(answer(503, {}), fake_sleep=True)
        self.assertEqual(outcome.paths, [REPOSITORY_PATH, REPOSITORY_PATH])
        self.assertEqual(outcome.create.await_count, 0)
        self.assertEqual(outcome.result.sources, [])
        self.assertEqual(outcome.run.trace.stop_reason, "repository_check_failed")
        self.assertEqual(outcome.result.limitations, [
            "Could not confirm that the repository is public, so no repository data is included. "
            "GitHub returned temporary HTTP 503.",
        ])

    async def test_anonymous_runs_skip_the_check_because_they_cannot_read_private_data(self):
        outcome = await run_agent(answer(body=PRIVATE), settings=SETTINGS)
        self.assertNotIn(REPOSITORY_PATH, outcome.paths)
        self.assertIsNotNone(outcome.result.content)

    async def test_resumed_tool_round_is_checked_before_pending_reads(self):
        outcome = await run_agent(answer(body=PRIVATE), checkpoint=resumed_checkpoint())
        self.assertEqual(outcome.paths, [REPOSITORY_PATH])
        self.assertEqual(outcome.create.await_count, 0)

    async def test_refusal_withholds_evidence_and_output_restored_from_a_checkpoint(self):
        outcome = await run_agent(answer(body=PRIVATE), checkpoint=resumed_checkpoint(restored_evidence=True))
        published = outcome.result.model_dump_json()
        for canary in ("RESTORED_EVIDENCE_CANARY", "RESTORED_LIMITATION_CANARY", "RESTORED_OUTPUT_CANARY"):
            self.assertNotIn(canary, published)
        self.assertEqual(outcome.result.limitations, [REPOSITORY_NOT_PUBLIC])
        # Spent budget is still reported.
        self.assertEqual(outcome.result.usage.model_requests, 1)

    async def test_restored_cooldown_is_waited_instead_of_failing_the_check(self):
        checkpoint = resumed_checkpoint(cooldown_until=time.time() + 0.05)
        outcome = await run_agent(answer(), checkpoint=checkpoint)
        self.assertEqual(outcome.paths[:2], [REPOSITORY_PATH, ISSUE_PATH])
        self.assertIsNotNone(outcome.result.content)

    async def test_cooldown_beyond_the_job_runtime_fails_without_waiting(self):
        checkpoint = resumed_checkpoint(cooldown_until=time.time() + 10_000)
        outcome = await run_agent(answer(), checkpoint=checkpoint)
        self.assertEqual(outcome.paths, [])
        self.assertEqual(outcome.run.trace.stop_reason, "repository_check_failed")

    async def test_confirmed_repository_is_not_rechecked_after_resume(self):
        outcome = await run_agent(answer(body=PRIVATE), checkpoint=resumed_checkpoint(repository_public=True))
        self.assertNotIn(REPOSITORY_PATH, outcome.paths)
        self.assertIn(ISSUE_PATH, outcome.paths)
        self.assertIsNotNone(outcome.result.content)

    async def test_restored_cooldown_after_confirmation_keeps_the_evidence(self):
        # First leg: confirm the repository, read the issue, then save while the catalog read is in flight.
        first = await run_agent(checkpoint=AgentCheckpoint.start("offline-signature"))
        interrupted = next(
            state for state in map(AgentCheckpoint.model_validate_json, first.saved_json)
            if state.in_flight == "tool" and state.pending_tools is not None
            and state.pending_tools.calls[0].name == "get_repo_labels"
        )
        self.assertEqual(interrupted.result.usage.model_requests, 0)
        assert interrupted.tools is not None
        # GitHub asked for a wait longer than the job runtime just before the Worker stopped.
        interrupted.tools.cooldown_until = time.time() + 600
        resumed = await run_agent(checkpoint=interrupted, responses=[simulated_response(content=final_content())])
        self.assertEqual(resumed.paths, [])  # The cooldown is honored.
        self.assertEqual([source.id for source in resumed.result.sources], ["S1"])
        self.assertIsNotNone(resumed.result.content)
        self.assertIn("get_repo_labels: GitHub's requested cooldown is still active.", resumed.result.limitations)
        # The resumed run finishes the interrupted catalog read once and repeats no earlier server read.
        self.assertEqual(resumed.result.usage.tool_calls, 3)
        calls = [item["call_id"] for item in resumed.create.call_args.kwargs["input"] if item.get("type") == "function_call"]
        self.assertEqual(calls, ["server_get_issue", "server_get_repo_labels"])
        self.assertNotEqual(resumed.run.trace.stop_reason, "repository_check_failed")


class RepositoryToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_tools_refuse_reads_until_the_repository_is_confirmed(self):
        paths = []
        github, _, _ = simulated_services([answer()], paths)
        async with github:
            tools = GitHubTools(github, TARGET, lambda text, limit: (text, False), require_public=True)
            with self.assertRaises(RepositoryNotPublicError):
                await tools.execute("get_issue", "{}")
            self.assertEqual(paths, [])
            await tools.require_public_repository()
            issue = await tools.execute("get_issue", "{}")
        self.assertEqual(paths, [REPOSITORY_PATH, ISSUE_PATH])
        self.assertEqual(issue["source"]["id"], "S1")


class RepositoryJobErrorTests(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, *answers):
        github, client, _ = simulated_services(answers, [])
        manager = JobManager(TOKEN_SETTINGS)
        gateway, model, encoding, lookups = offline_patches(github, client)
        with gateway, model, encoding, lookups, patch("backend.app.agent.asyncio.sleep", new_callable=AsyncMock):
            job = await manager.create(TARGET)
            assert job.task is not None
            await job.task
        return job.snapshot

    async def test_job_errors_name_the_repository_rule(self):
        refused = await self.run_job(answer(body=PRIVATE))
        self.assertEqual((refused.status, refused.error), ("failed", REPOSITORY_NOT_PUBLIC))
        unconfirmed = await self.run_job(answer(503, {}))
        self.assertEqual((unconfirmed.status, unconfirmed.error), ("failed", REPOSITORY_CHECK_FAILED))


if __name__ == "__main__":
    unittest.main()
