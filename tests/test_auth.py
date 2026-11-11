"""Exercise the GitHub login boundary and durable analysis ownership."""

import asyncio
from base64 import urlsafe_b64encode
from contextlib import AsyncExitStack
from dataclasses import replace
from hashlib import sha256
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx
import psycopg

from backend.app import config
from backend.app.auth import TOKEN_URL, USER_URL
from backend.app.config import GITHUB_CALLBACK_PATH, GitHubOAuthConfig, Settings
from backend.app.main import create_app
from backend.app.worker import AnalysisWorker
from test_mvp import SETTINGS, TARGET, offline_result
from test_persistence import api_client, saved_snapshot


ORIGIN = "http://127.0.0.1:3000"
CALLBACK = ORIGIN + GITHUB_CALLBACK_PATH
OAUTH = GitHubOAuthConfig("fixture-client-id", "fixture-client-secret", CALLBACK)
LOGIN = "/api/v1/auth/github/login"
SESSION = "/api/v1/auth/session"
LOGOUT = "/api/v1/auth/logout"
ANALYSES = "/api/v1/analyses"
STATE_COOKIE = "issuelens_oauth_state"
SESSION_COOKIE = "issuelens_session"


class OAuthConfigurationTests(unittest.TestCase):
    def test_local_mode_exposes_no_login_and_keeps_the_existing_api(self):
        with TestClient(create_app(Settings())) as client:
            self.assertEqual(client.get(SESSION).json(), {"enabled": False, "user": None})
            self.assertEqual(client.get(LOGIN).status_code, 404)
            self.assertEqual(client.get(f"{ANALYSES}/missing").status_code, 404)

    def test_incomplete_oauth_configuration_fails_closed_and_hides_the_secret(self):
        values = {
            "GITHUB_OAUTH_CLIENT_ID": OAUTH.client_id,
            "GITHUB_OAUTH_CLIENT_SECRET": OAUTH.client_secret,
            "GITHUB_OAUTH_CALLBACK_URL": CALLBACK,
        }
        with patch.dict(os.environ, values, clear=True), patch.object(config, "load_dotenv"):
            with self.assertRaisesRegex(ValueError, "requires DATABASE_URL"):
                Settings.from_env()
            os.environ["GITHUB_OAUTH_CLIENT_ID"] = ""
            with self.assertRaisesRegex(ValueError, "Set both") as error:
                Settings.from_env()
            self.assertNotIn(OAUTH.client_secret, str(error.exception))
        self.assertNotIn(OAUTH.client_secret, repr(OAUTH))
        with self.assertRaisesRegex(ValueError, "HTTPS or loopback HTTP"):
            replace(OAUTH, callback_url="http://example.com" + GITHUB_CALLBACK_PATH)


@unittest.skipUnless(os.getenv("ISSUELENS_TEST_DATABASE_URL"), "OAuth integration needs a dedicated PostgreSQL database.")
class PostgresOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database_url = os.environ["ISSUELENS_TEST_DATABASE_URL"]
        if not urlsplit(self.database_url).path.endswith("_test"):
            self.skipTest("Use a dedicated database whose name ends in _test.")
        self.settings = replace(SETTINGS, database_url=self.database_url, github_oauth=OAUTH)
        self.job_ids = []
        self.state_hashes = []
        self.session_hashes = []
        self.profiles = {}
        self.codes = {}
        self.exchanges = 0
        self.addAsyncCleanup(self.clean_database)
        self.stack = await AsyncExitStack().__aenter__()
        self.addAsyncCleanup(self.stack.aclose)
        self.app = create_app(self.settings, oauth_transport=httpx.MockTransport(self.github))
        await self.stack.enter_async_context(self.app.router.lifespan_context(self.app))

    async def clean_database(self):
        async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
            if self.job_ids:
                await connection.execute("DELETE FROM issuelens_analyses WHERE job_id = ANY(%s)", (self.job_ids,))
            if self.state_hashes:
                await connection.execute(
                    "DELETE FROM issuelens_oauth_requests WHERE state_hash = ANY(%s)", (self.state_hashes,),
                )
            if self.session_hashes:
                await connection.execute(
                    "DELETE FROM issuelens_sessions WHERE token_hash = ANY(%s)", (self.session_hashes,),
                )

    def github(self, request):
        if str(request.url) == TOKEN_URL:
            self.exchanges += 1
            form = parse_qs(request.content.decode())
            user, challenge = self.codes.pop(form["code"][0])
            self.assertEqual(form["client_secret"], [OAUTH.client_secret])
            self.assertEqual(form["redirect_uri"], [CALLBACK])
            actual = urlsafe_b64encode(sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            self.assertEqual(actual, challenge)
            token = f"fixture-upstream-token-{user['id']}"
            self.profiles[token] = user
            return httpx.Response(200, json={"access_token": token})
        self.assertEqual(str(request.url), USER_URL)
        profile = self.profiles[request.headers["authorization"].removeprefix("Bearer ")]
        return httpx.Response(200, json=profile)

    async def browser(self, *, cookies=None, app=None):
        return await self.stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app or self.app), base_url=ORIGIN,
            headers={"Origin": ORIGIN}, cookies=cookies,
        ))

    async def begin(self, browser, *, job_id=None):
        response = await browser.get(LOGIN, params={"analysis": job_id} if job_id else None)
        self.assertEqual(response.status_code, 303)
        location = urlsplit(response.headers["location"])
        self.assertEqual((location.hostname, location.path), ("github.com", "/login/oauth/authorize"))
        query = parse_qs(location.query)
        state = query["state"][0]
        self.state_hashes.append(sha256(state.encode()).hexdigest())
        self.assertEqual(browser.cookies.get(STATE_COOKIE), state)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [CALLBACK])
        self.assertNotIn("client_secret", query)
        return state, query["code_challenge"][0]

    async def sign_in(self, browser, user_id, login, *, job_id=None):
        state, challenge = await self.begin(browser, job_id=job_id)
        code = f"fixture-code-{state}"
        self.codes[code] = ({"id": user_id, "login": login}, challenge)
        response = await browser.get(GITHUB_CALLBACK_PATH, params={"state": state, "code": code})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/?analysis={job_id}" if job_id else "/")
        self.assertNotIn("fixture-upstream-token", str(response.headers))
        cookie = browser.cookies.get(SESSION_COOKIE)
        self.assertIsNotNone(cookie)
        self.session_hashes.append(sha256(cookie.encode()).hexdigest())
        self.assertNotIn(STATE_COOKIE, browser.cookies)
        self.assertEqual((await browser.get(SESSION)).json(), {
            "enabled": True, "user": {"id": user_id, "login": login},
        })

    async def submit(self, browser, issue_number=42):
        response = await browser.post(ANALYSES, json={"issue_url": f"https://github.com/example/project/issues/{issue_number}"})
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["job_id"]
        self.job_ids.append(job_id)
        self.assertNotIn("owner_github_id", response.json())
        return f"{ANALYSES}/{job_id}"

    async def assert_isolated(self, browser, other_path):
        for suffix in ("", "/events", "/trace"):
            response = await browser.get(other_path + suffix)
            self.assertEqual(response.status_code, 404)
            missing = await browser.get(f"{ANALYSES}/missing{suffix}")
            self.assertEqual(response.json(), missing.json())
        self.assertEqual((await browser.delete(other_path)).status_code, 404)

    async def test_two_github_identities_can_only_access_their_own_analyses(self):
        alice, bob, guest = await self.browser(), await self.browser(), await self.browser()
        await self.sign_in(alice, 4101, "alice-fixture")
        await self.sign_in(bob, 4102, "bob-fixture")
        paths = [await self.submit(alice, 42), await self.submit(bob, 43)]
        for path in paths:
            self.assertEqual((await guest.get(path)).status_code, 401)
            self.assertEqual((await guest.get(path + "/events")).status_code, 401)
            self.assertEqual((await guest.get(path + "/trace")).status_code, 401)
            self.assertEqual((await guest.delete(path)).status_code, 401)
        self.assertEqual((await guest.post(ANALYSES, json={"issue_url": TARGET.url})).status_code, 401)
        await self.assert_isolated(alice, paths[1])
        await self.assert_isolated(bob, paths[0])

        async def complete(run):
            run.trace.begin("model", "responses.create", 1, None)
            run.trace.finish("succeeded", reason="Fixture analysis.")
            result = offline_result()
            result.content.draft_reply = f"Fixture reply for issue {run.target.number}."
            return result

        stop = asyncio.Event()
        with patch("backend.app.jobs.AgentRun.run", new=complete):
            worker = asyncio.create_task(AnalysisWorker(self.settings).run(stop))
            try:
                for client, path, issue_number in zip((alice, bob), paths, (42, 43), strict=True):
                    async with asyncio.timeout(5):
                        while True:
                            snapshot = await client.get(path)
                            self.assertEqual(snapshot.status_code, 200)
                            if snapshot.json()["status"] == "completed":
                                break
                            await asyncio.sleep(0.02)
                    self.assertEqual(snapshot.json()["result"]["content"]["draft_reply"], f"Fixture reply for issue {issue_number}.")
                    self.assertIn("event: done", (await client.get(path + "/events")).text)
                    self.assertEqual((await client.get(path + "/trace")).json()["attempts"][0]["outcome"], "succeeded")
                await self.assert_isolated(alice, paths[1])
                await self.assert_isolated(bob, paths[0])
            finally:
                stop.set()
                await worker

        queued = await self.submit(alice, 44)
        self.assertEqual((await bob.delete(queued)).status_code, 404)
        self.assertEqual((await alice.delete(queued, headers={"Origin": "https://elsewhere.example"})).status_code, 403)
        self.assertEqual((await alice.delete(queued)).json()["status"], "cancelled")

        restarted = create_app(self.settings)
        await self.stack.enter_async_context(restarted.router.lifespan_context(restarted))
        reopened = await self.browser(cookies={SESSION_COOKIE: alice.cookies.get(SESSION_COOKIE)}, app=restarted)
        self.assertEqual((await reopened.get(paths[0])).status_code, 200)
        self.assertEqual((await reopened.get(paths[1])).status_code, 404)

        signed_out = await alice.post(LOGOUT)
        self.assertEqual(signed_out.status_code, 204)
        self.assertEqual((await reopened.get(paths[0])).status_code, 401)
        await self.sign_in(alice, 4101, "alice-renamed", job_id=paths[0].rsplit("/", 1)[-1])
        self.assertEqual((await alice.get(paths[0])).status_code, 200)

    async def test_oauth_callback_is_bound_to_the_browser_and_consumed_once(self):
        alice, other = await self.browser(), await self.browser()
        state, challenge = await self.begin(alice)
        params = {"state": state, "code": "fixture-callback-code"}
        self.codes[params["code"]] = ({"id": 4103, "login": "state-fixture"}, challenge)
        rejected = await other.get(GITHUB_CALLBACK_PATH, params=params)
        self.assertEqual(rejected.headers["location"], "/?auth_error=github")
        self.assertEqual(self.exchanges, 0)
        accepted = await alice.get(GITHUB_CALLBACK_PATH, params=params)
        self.assertEqual(accepted.headers["location"], "/")
        self.assertEqual(self.exchanges, 1)
        self.session_hashes.append(sha256(alice.cookies.get(SESSION_COOKIE).encode()).hexdigest())
        alice.cookies.set(STATE_COOKIE, state)
        replay = await alice.get(GITHUB_CALLBACK_PATH, params=params)
        self.assertEqual(replay.headers["location"], "/?auth_error=github")
        self.assertEqual(self.exchanges, 1)

    async def test_legacy_records_stay_in_local_mode_and_owned_records_are_private(self):
        legacy = saved_snapshot()
        self.job_ids.append(legacy.job_id)
        async with await psycopg.AsyncConnection.connect(self.database_url) as connection:
            await connection.execute(
                "INSERT INTO issuelens_analyses (job_id, snapshot) VALUES (%s, %s::jsonb)",
                (legacy.job_id, legacy.model_dump_json()),
            )
        alice = await self.browser()
        await self.sign_in(alice, 4101, "alice-fixture")
        owned = await self.submit(alice)
        await self.assert_isolated(alice, f"{ANALYSES}/{legacy.job_id}")
        async with api_client(replace(self.settings, github_oauth=None)) as local:
            self.assertEqual((await local.get(f"{ANALYSES}/{legacy.job_id}")).status_code, 200)
            await self.assert_isolated(local, owned)
