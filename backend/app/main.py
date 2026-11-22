"""FastAPI endpoints for creating, observing and cancelling local analyses."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import logging

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from backend.app.auth import AuthSession, GitHubAuth, PRIVATE_HEADERS, redact_oauth_callback
from backend.app.config import GITHUB_CALLBACK_PATH, Settings
from backend.app.jobs import JobService, MemoryJobService, PostgresJobQueue, QueueFullError
from backend.app.models import AnalysisRequest, Health, IssueTarget, JobSnapshot, TERMINAL_STATUSES
from backend.app.storage import PostgresAnalysisStore, StorageError, STORAGE_UNAVAILABLE
from backend.app.trace import AnalysisTrace


ANALYSIS_NOT_FOUND = "Analysis not found. Check the task URL and the configured storage."
HEARTBEAT_SECONDS = 15


def create_app(
    settings: Settings | None = None, *, oauth_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the API. Serve it with `uvicorn --factory backend.app.main:create_app`.

    There is no module-level app, so importing this module (as tests do) never reads
    .env or loads credentials into the process environment.
    """
    config = settings or Settings.from_env()
    database = (
        PostgresAnalysisStore(config.database_url, auth_enabled=config.github_oauth is not None)
        if config.database_url else None
    )
    # The storage mode is chosen here once; the routes below only use the JobService interface.
    jobs: JobService = PostgresJobQueue(config, database) if database is not None else MemoryJobService(config)
    auth: GitHubAuth | None = None
    if config.github_oauth is not None:
        if database is None:
            raise ValueError("GitHub sign-in requires DATABASE_URL.")
        auth = GitHubAuth(config.github_oauth, database, oauth_transport)
        logging.getLogger("uvicorn.access").addFilter(redact_oauth_callback)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await jobs.start()
        try:
            yield
        finally:
            await jobs.close()

    app = FastAPI(title="IssueLens", version="0.1.0", lifespan=lifespan)
    app.state.jobs = jobs

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": STORAGE_UNAVAILABLE})

    async def analysis_owner(request: Request, response: Response) -> int | None:
        if auth is None:
            return None
        user = await auth.user(request)
        if user is None:
            raise HTTPException(401, "Sign in with GitHub to access analyses.", headers=PRIVATE_HEADERS)
        if request.method in {"POST", "DELETE"}:
            auth.require_write_origin(request)
        response.headers.update(PRIVATE_HEADERS)
        return user.id

    def found(snapshot: JobSnapshot | None) -> JobSnapshot:
        if snapshot is None:
            raise HTTPException(404, ANALYSIS_NOT_FOUND, headers=PRIVATE_HEADERS)
        return snapshot

    @app.get("/api/v1/auth/session", response_model=AuthSession)
    async def auth_session(request: Request, response: Response) -> AuthSession:
        response.headers.update(PRIVATE_HEADERS)
        return AuthSession(enabled=auth is not None, user=await auth.user(request) if auth is not None else None)

    if auth is not None:
        app.add_api_route("/api/v1/auth/github/login", auth.login, methods=["GET"], include_in_schema=False)
        app.add_api_route(GITHUB_CALLBACK_PATH, auth.callback, methods=["GET"], include_in_schema=False)
        app.add_api_route("/api/v1/auth/logout", auth.logout, methods=["POST"], status_code=204)

    @app.get("/api/v1/health", response_model=Health)
    async def health() -> Health:
        return Health(
            status="ok", openai_configured=bool(config.openai_api_key),
            github_authenticated=bool(config.github_token),
            storage="postgresql" if database is not None else "in-memory", model=config.model,
        )

    @app.post("/api/v1/analyses", response_model=JobSnapshot, status_code=status.HTTP_202_ACCEPTED)
    async def create_analysis(body: AnalysisRequest, owner_id: int | None = Depends(analysis_owner)) -> JobSnapshot:
        if not config.openai_api_key:
            raise HTTPException(503, "Set OPENAI_API_KEY in the server's .env file before starting an analysis.")
        try:
            return await jobs.create(IssueTarget.from_url(body.issue_url), owner_id=owner_id)
        except QueueFullError as exc:
            raise HTTPException(429, str(exc)) from exc

    @app.get("/api/v1/analyses/{job_id}", response_model=JobSnapshot)
    async def get_analysis(job_id: str, owner_id: int | None = Depends(analysis_owner)) -> JobSnapshot:
        return found(await jobs.get(job_id, owner_id=owner_id))

    @app.get("/api/v1/analyses/{job_id}/trace", response_model=AnalysisTrace)
    async def get_analysis_trace(job_id: str, owner_id: int | None = Depends(analysis_owner)) -> AnalysisTrace:
        trace = await jobs.get_trace(job_id, owner_id=owner_id)
        if trace is None:
            raise HTTPException(404, ANALYSIS_NOT_FOUND, headers=PRIVATE_HEADERS)
        return trace

    @app.delete("/api/v1/analyses/{job_id}", response_model=JobSnapshot)
    async def cancel_analysis(
        job_id: str, response: Response, owner_id: int | None = Depends(analysis_owner),
    ) -> JobSnapshot:
        snapshot = found(await jobs.cancel(job_id, owner_id=owner_id))
        if snapshot.status == "running":
            # PostgreSQL mode: the Worker stops the run when it reads the request.
            response.status_code = status.HTTP_202_ACCEPTED
        return snapshot

    @app.get("/api/v1/analyses/{job_id}/events")
    async def analysis_events(
        job_id: str, request: Request, after: int = Query(default=0, ge=0),
        owner_id: int | None = Depends(analysis_owner),
    ) -> StreamingResponse:
        snapshot = found(await jobs.get(job_id, owner_id=owner_id))
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = after

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor, snapshot
            loop = asyncio.get_running_loop()
            next_heartbeat = loop.time() + HEARTBEAT_SECONDS
            while True:
                for event in snapshot.events:
                    if event.id > cursor:
                        yield f"id: {event.id}\ndata: {event.model_dump_json()}\n\n"
                        cursor = event.id
                if snapshot.status in TERMINAL_STATUSES:
                    payload = json.dumps(snapshot.model_dump(mode="json"))
                    yield f"event: done\ndata: {payload}\n\n"
                    break
                try:
                    latest = await jobs.wait_for_update(
                        job_id, owner_id=owner_id, after=cursor, timeout=max(0, next_heartbeat - loop.time()),
                    )
                    if auth is not None:
                        # Stop when the session ends or another account signs in.
                        user = await auth.user(request)
                        if user is None or user.id != owner_id:
                            return
                except StorageError:
                    return  # The browser can reconnect and retry a read.
                if latest is None:
                    return
                snapshot = latest
                if loop.time() >= next_heartbeat:
                    yield ": keep-alive\n\n"
                    next_heartbeat = loop.time() + HEARTBEAT_SECONDS

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "private, no-store, no-transform" if auth else "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })

    return app
