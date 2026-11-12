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
from backend.app.jobs import Job, JobManager, PostgresJobQueue, QueueFullError
from backend.app.models import AnalysisRequest, IssueTarget, JobSnapshot, TERMINAL_STATUSES
from backend.app.storage import AnalysisStore, POLL_SECONDS, PostgresAnalysisStore, StorageError, STORAGE_UNAVAILABLE
from backend.app.trace import AnalysisTrace


ANALYSIS_NOT_FOUND = "Analysis not found. Check the task URL and the configured storage."


def create_app(
    settings: Settings | None = None, *, store: AnalysisStore | None = None,
    oauth_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the API. Serve it with `uvicorn --factory backend.app.main:create_app`.

    There is no module-level app, so importing this module (as tests do) never reads
    .env or loads credentials into the process environment.
    """
    config = settings or Settings.from_env()
    database = (
        PostgresAnalysisStore(config.database_url, auth_enabled=config.github_oauth is not None)
        if store is None and config.database_url else None
    )
    manager = PostgresJobQueue(config, database) if database is not None else JobManager(config, store)
    auth: GitHubAuth | None = None
    if config.github_oauth is not None:
        if database is None:
            raise ValueError("GitHub sign-in requires DATABASE_URL.")
        auth = GitHubAuth(config.github_oauth, database, oauth_transport)
        logging.getLogger("uvicorn.access").addFilter(redact_oauth_callback)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="IssueLens", version="0.1.0", lifespan=lifespan)
    app.state.manager = manager

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

    async def find_job(job_id: str, owner_id: int | None) -> Job:
        job = (
            await manager.get(job_id, owner_id=owner_id) if isinstance(manager, PostgresJobQueue)
            else await manager.get(job_id)
        )
        if job is None:
            raise HTTPException(404, ANALYSIS_NOT_FOUND, headers=PRIVATE_HEADERS)
        return job

    @app.get("/api/v1/auth/session", response_model=AuthSession)
    async def auth_session(request: Request, response: Response) -> AuthSession:
        response.headers.update(PRIVATE_HEADERS)
        return AuthSession(enabled=auth is not None, user=await auth.user(request) if auth is not None else None)

    if auth is not None:
        app.add_api_route("/api/v1/auth/github/login", auth.login, methods=["GET"], include_in_schema=False)
        app.add_api_route(GITHUB_CALLBACK_PATH, auth.callback, methods=["GET"], include_in_schema=False)
        app.add_api_route("/api/v1/auth/logout", auth.logout, methods=["POST"], status_code=204)

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok", "openai_configured": bool(config.openai_api_key),
            "github_authenticated": bool(config.github_token),
            "storage": "postgresql" if manager.store is not None else "in-memory", "model": config.model,
        }

    @app.post("/api/v1/analyses", response_model=JobSnapshot, status_code=status.HTTP_202_ACCEPTED)
    async def create_analysis(body: AnalysisRequest, owner_id: int | None = Depends(analysis_owner)) -> JobSnapshot:
        if not config.openai_api_key:
            raise HTTPException(503, "Set OPENAI_API_KEY in the server's .env file before starting an analysis.")
        try:
            target = IssueTarget.from_url(body.issue_url)
            job = (
                await manager.create(target, owner_id=owner_id) if isinstance(manager, PostgresJobQueue)
                else await manager.create(target)
            )
        except QueueFullError as exc:
            raise HTTPException(429, str(exc)) from exc
        return job.snapshot

    @app.get("/api/v1/analyses/{job_id}", response_model=JobSnapshot)
    async def get_analysis(job_id: str, owner_id: int | None = Depends(analysis_owner)) -> JobSnapshot:
        return (await find_job(job_id, owner_id)).snapshot

    @app.get("/api/v1/analyses/{job_id}/trace", response_model=AnalysisTrace)
    async def get_analysis_trace(job_id: str, owner_id: int | None = Depends(analysis_owner)) -> AnalysisTrace:
        trace = (
            await manager.get_trace(job_id, owner_id=owner_id) if isinstance(manager, PostgresJobQueue)
            else await manager.get_trace(job_id)
        )
        if trace is None:
            raise HTTPException(404, ANALYSIS_NOT_FOUND, headers=PRIVATE_HEADERS)
        return trace

    @app.delete("/api/v1/analyses/{job_id}", response_model=JobSnapshot)
    async def cancel_analysis(
        job_id: str, response: Response, owner_id: int | None = Depends(analysis_owner),
    ) -> JobSnapshot:
        job = await find_job(job_id, owner_id)
        if isinstance(manager, PostgresJobQueue):
            await manager.cancel(job, owner_id=owner_id)
        else:
            await manager.cancel(job)
        if job.snapshot.status == "running":
            response.status_code = status.HTTP_202_ACCEPTED
        return job.snapshot

    @app.get("/api/v1/analyses/{job_id}/events")
    async def analysis_events(
        job_id: str, request: Request, after: int = Query(default=0, ge=0),
        owner_id: int | None = Depends(analysis_owner),
    ) -> StreamingResponse:
        job = await find_job(job_id, owner_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = after

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            loop = asyncio.get_running_loop()
            next_heartbeat = loop.time() + 15
            while True:
                job.changed.clear()
                for event in job.snapshot.events:
                    if event.id > cursor:
                        yield f"id: {event.id}\ndata: {event.model_dump_json()}\n\n"
                        cursor = event.id
                if job.snapshot.status in TERMINAL_STATUSES:
                    payload = json.dumps(job.snapshot.model_dump(mode="json"))
                    yield f"event: done\ndata: {payload}\n\n"
                    break
                if database is not None:
                    await asyncio.sleep(POLL_SECONDS)
                    try:
                        if auth is not None:
                            user = await auth.user(request)
                            if user is None or user.id != owner_id:
                                return
                        snapshot = await database.get(job_id, owner_id=owner_id)
                    except StorageError:
                        return  # The browser can reconnect and retry a read.
                    if snapshot is None:
                        return
                    job.snapshot = snapshot
                    if loop.time() >= next_heartbeat:
                        yield ": keep-alive\n\n"
                        next_heartbeat = loop.time() + 15
                else:
                    try:
                        await asyncio.wait_for(job.changed.wait(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "private, no-store, no-transform" if auth else "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })

    return app
