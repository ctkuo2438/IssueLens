"""GitHub OAuth and revocable browser sessions backed by PostgreSQL."""

from base64 import urlsafe_b64encode
from datetime import timedelta
from hashlib import sha256
import logging
import re
import secrets
from urllib.parse import urlencode

from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
import httpx

from backend.app.config import GITHUB_CALLBACK_PATH, GitHubOAuthConfig
from backend.app.models import ApiModel, GitHubUser
from backend.app.storage import PostgresAnalysisStore


AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
LOGIN_LIFETIME = timedelta(minutes=10)
SESSION_LIFETIME = timedelta(hours=8)
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
JOB_ID_FORMAT = re.compile(r"[a-f0-9]{32}")


class AuthSession(ApiModel):
    enabled: bool
    user: GitHubUser | None = None


def redact_oauth_callback(record: logging.LogRecord) -> bool:
    # Uvicorn's access log path includes the query string.
    args = record.args
    if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
        if args[2].partition("?")[0].rstrip("/") == GITHUB_CALLBACK_PATH:
            record.args = (*args[:2], GITHUB_CALLBACK_PATH, *args[3:])
    return True


class GitHubAuth:
    def __init__(
        self, config: GitHubOAuthConfig, store: PostgresAnalysisStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.transport = transport
        self.origin = config.origin
        self.secure = self.origin.startswith("https://")
        prefix = "__Host-" if self.secure else ""
        # A login attempt lives only in these HttpOnly cookies until GitHub confirms the user.
        self.state_cookie = prefix + "issuelens_oauth_state"
        self.verifier_cookie = prefix + "issuelens_oauth_verifier"
        self.return_cookie = prefix + "issuelens_oauth_return"
        self.session_cookie = prefix + "issuelens_session"

    def require_write_origin(self, request: Request) -> None:
        if request.headers.get("origin") != self.origin:
            raise HTTPException(403, "The request must come from the IssueLens website.", headers=PRIVATE_HEADERS)

    def _session_hash(self, request: Request) -> str | None:
        token = request.cookies.get(self.session_cookie)
        return sha256(token.encode()).hexdigest() if token and TOKEN_FORMAT.fullmatch(token) else None

    async def user(self, request: Request) -> GitHubUser | None:
        token_hash = self._session_hash(request)
        return await self.store.session_user(token_hash) if token_hash else None

    def _set_cookie(self, response: Response, name: str, value: str, lifetime: timedelta) -> None:
        response.set_cookie(
            name, value, max_age=int(lifetime.total_seconds()), path="/",
            secure=self.secure, httponly=True, samesite="lax",
        )

    def _clear_cookie(self, response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=self.secure, httponly=True, samesite="lax")

    @staticmethod
    def _home(job_id: str | None = None, *, failed: bool = False) -> RedirectResponse:
        params = {}
        if job_id:
            params["analysis"] = job_id
        if failed:
            params["auth_error"] = "github"
        return RedirectResponse(f"/?{urlencode(params)}" if params else "/", status_code=303, headers=PRIVATE_HEADERS)

    def _clear_login_cookies(self, response: Response) -> None:
        for name in (self.state_cookie, self.verifier_cookie, self.return_cookie):
            self._clear_cookie(response, name)

    async def login(self, analysis: str | None = None) -> RedirectResponse:
        # No database write: anyone can call this without signing in.
        return_job_id = analysis if analysis and JOB_ID_FORMAT.fullmatch(analysis) else None
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(32)
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode("ascii")
        query = urlencode({
            "client_id": self.config.client_id,
            "redirect_uri": self.config.callback_url,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        })
        response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=303, headers=PRIVATE_HEADERS)
        self._set_cookie(response, self.state_cookie, state, LOGIN_LIFETIME)
        self._set_cookie(response, self.verifier_cookie, verifier, LOGIN_LIFETIME)
        if return_job_id:
            self._set_cookie(response, self.return_cookie, return_job_id, LOGIN_LIFETIME)
        else:
            self._clear_cookie(response, self.return_cookie)
        return response

    def _failed_login(self, job_id: str | None = None) -> RedirectResponse:
        response = self._home(job_id, failed=True)
        self._clear_login_cookies(response)
        return response

    async def callback(self, request: Request) -> RedirectResponse:
        state = request.query_params.get("state")
        state_cookie = request.cookies.get(self.state_cookie)
        verifier = request.cookies.get(self.verifier_cookie)
        if (
            not state or not TOKEN_FORMAT.fullmatch(state) or not state_cookie
            or not secrets.compare_digest(state.encode(), state_cookie.encode())
            or not verifier or not TOKEN_FORMAT.fullmatch(verifier)
        ):
            return self._failed_login()
        state_hash = sha256(state.encode()).hexdigest()
        # A read, not a write: a used attempt is refused before contacting GitHub again.
        if await self.store.login_used(state_hash):
            return self._failed_login()
        stored_job_id = request.cookies.get(self.return_cookie)
        return_job_id = stored_job_id if stored_job_id and JOB_ID_FORMAT.fullmatch(stored_job_id) else None
        code = request.query_params.get("code")
        user = await self._exchange_identity(code, verifier) if code and len(code) <= 512 else None
        if user is None:
            return self._failed_login(return_job_id)
        token = secrets.token_urlsafe(32)
        # Marking the attempt used in the session transaction keeps it single-use even when
        # two callbacks race past the read above.
        created = await self.store.create_session(
            sha256(token.encode()).hexdigest(), user, SESSION_LIFETIME, self._session_hash(request),
            login_hash=state_hash, login_lifetime=LOGIN_LIFETIME,
        )
        if not created:
            return self._failed_login(return_job_id)
        response = self._home(return_job_id)
        self._clear_login_cookies(response)
        self._set_cookie(response, self.session_cookie, token, SESSION_LIFETIME)
        return response

    async def _exchange_identity(self, code: str, verifier: str) -> GitHubUser | None:
        try:
            async with httpx.AsyncClient(timeout=10, transport=self.transport, follow_redirects=False) as client:
                token_response = await client.post(TOKEN_URL, data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "redirect_uri": self.config.callback_url,
                    "code": code,
                    "code_verifier": verifier,
                }, headers={"Accept": "application/json"})
                token_response.raise_for_status()
                payload = token_response.json()
                token = payload.get("access_token") if isinstance(payload, dict) else None
                if not isinstance(token, str) or not token:
                    return None
                profile = await client.get(USER_URL, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "IssueLens",
                })
                profile.raise_for_status()
                return GitHubUser.model_validate(profile.json())
        except (httpx.HTTPError, ValueError):
            return None

    async def logout(self, request: Request) -> Response:
        self.require_write_origin(request)
        token_hash = self._session_hash(request)
        if token_hash:
            await self.store.delete_session(token_hash)
        response = Response(status_code=204, headers=PRIVATE_HEADERS)
        self._clear_cookie(response, self.session_cookie)
        self._clear_login_cookies(response)
        return response
