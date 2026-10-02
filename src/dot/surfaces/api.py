"""HTTP surface. Routes write the inbox and stream events. They never run an agent."""

from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast, get_args

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from dot.assembly import GraphRuntime, build_dot_agent, build_graph_runtime
from dot.config import Settings, get_settings
from dot.jobs.store import JobStore, MemoryJobStore, PostgresJobStore
from dot.memory.episodes import CorrectionBody, record_correction
from dot.memory.reflection import MemoryFiles
from dot.memory.versions import accept_reviewed, discard, rollback
from dot.packs.loader import REPO_ROOT, PackLoadError, load_pack
from dot.persistence.db import (
    ApprovalConflict,
    Dot,
    MemoryConflict,
    MemoryRepositories,
    NotFound,
    PostgresRepositories,
    Repositories,
    open_repositories,
)
from dot.proactive.scheduler import trigger
from dot.runtime.locks import DotLocks
from dot.runtime.turns import EventKind, InMemoryEventChannel, PgEventChannel, TurnEvent
from dot.safety.approvals import ReviewDecision, approvers, decide
from dot.surfaces.dots import create_dot, dot_view, post_message, read_thread
from dot.surfaces.identity import IapVerifier, SchedulerVerifier, install_identity, principal
from dot.surfaces.views import approval_view, audit_view, finding_view, job_view, memory_version_view

# Keepalive comments stop proxies from closing an idle stream; EventSource ignores them.
_SSE_PING_S = 15
# Cloud Scheduler names the slot it fired for, so a retried attempt keeps its slot.
_SCHEDULE_TIME_HEADER = "x-cloudscheduler-scheduletime"


class CreateDotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: str = Field(min_length=1)
    owner_user_id: str = Field(default="local", min_length=1)
    display_name: str | None = None


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    profile: str = "chat"


class Surface:
    def __init__(
        self,
        settings: Settings,
        repos: Repositories,
        runtime: GraphRuntime,
        events: InMemoryEventChannel | PgEventChannel,
        jobs: JobStore | None,
        *,
        model: BaseChatModel | None = None,
        owns_repos: bool = False,
        owns_runtime: bool = False,
    ) -> None:
        self.settings = settings
        self.repos = repos
        self.runtime = runtime
        self.events = events
        self.jobs = jobs
        self.model = model
        self.owns_repos = owns_repos
        self.owns_runtime = owns_runtime

    def close(self) -> None:
        if self.owns_runtime:
            self.runtime.close()
        if self.owns_repos:
            self.repos.close()


def create_app(
    settings: Settings | None = None,
    *,
    repos: Repositories | None = None,
    runtime: GraphRuntime | None = None,
    events: InMemoryEventChannel | PgEventChannel | None = None,
    model: BaseChatModel | None = None,
    iap_verifier: IapVerifier | None = None,
    scheduler_verifier: SchedulerVerifier | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    owns_repos = repos is None
    owns_runtime = runtime is None
    if repos is None:
        repos = open_repositories(settings.database_url)
    if runtime is None:
        runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    if events is None:
        pool = repos.pool if isinstance(repos, PostgresRepositories) else None
        events = PgEventChannel(pool) if pool is not None and settings.database_url else InMemoryEventChannel()
    surface = Surface(
        settings,
        repos,
        runtime,
        events,
        runtime.jobs or _job_store(repos),
        model=model,
        owns_repos=owns_repos,
        owns_runtime=owns_runtime,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        try:
            yield
        finally:
            surface.close()

    app = FastAPI(title="open dot", lifespan=lifespan)
    app.state.surface = surface
    install_identity(app, settings, repos, iap_verifier)
    if scheduler_verifier is None and settings.scheduler_audience and settings.scheduler_invoker:
        scheduler_verifier = SchedulerVerifier(settings.scheduler_audience, settings.scheduler_invoker)

    def viewer(request: Request, dot_id: str) -> Dot:
        """The dot, when the verified caller is its owner or a pack approver."""
        user_id = principal(request)
        if user_id is None:
            raise HTTPException(401, "authenticated identity required")
        dot = surface.repos.get_dot(dot_id)
        if user_id != dot.owner_user_id and user_id not in approvers(dot.pack_name):
            raise HTTPException(403, "access to this dot is not permitted")
        return dot

    @app.exception_handler(NotFound)
    def not_found(_request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(PackLoadError)
    def bad_pack(_request: Request, exc: PackLoadError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    def bad_request(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if settings.slack_mode == "http" and settings.slack_bot_token and settings.slack_signing_secret:
        _mount_slack(app, settings, surface)

    @app.get("/me")
    def me(request: Request) -> dict[str, Any]:
        user_id = principal(request)
        if user_id is None:
            raise HTTPException(401, "authenticated identity required")
        return {"user_id": user_id}

    @app.get("/packs")
    def packs() -> dict[str, Any]:
        return {"packs": _packs()}

    @app.get("/dots")
    def my_dots(request: Request) -> dict[str, Any]:
        user_id = principal(request)
        if user_id is None:
            raise HTTPException(401, "authenticated identity required")
        return {"dots": [dot_view(dot) for dot in surface.repos.list_dots_for_owner(user_id)]}

    @app.post("/approvals/{approval_id}", status_code=202)
    def approval_decision(approval_id: str, body: ReviewDecision, request: Request) -> dict[str, Any]:
        # Only trusted authentication middleware may set this principal.
        user_id = principal(request)
        if user_id is None:
            raise HTTPException(401, "authenticated identity required")
        try:
            card = decide(surface.repos, approval_id, user_id, body, redactor=surface.runtime.redactor)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ApprovalConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        surface.events.publish(
            TurnEvent(card.dot_id, "approval", {"approval_id": card.approval_id, "status": card.status})
        )
        return {"approval_id": card.approval_id, "status": card.status}

    @app.post("/dots", status_code=201)
    def create(body: CreateDotBody) -> dict[str, Any]:
        dot = create_dot(surface.repos, surface.runtime, body.pack, body.owner_user_id, body.display_name)
        return dot_view(dot)

    @app.get("/dots/{dot_id}")
    def get_dot(dot_id: str) -> dict[str, Any]:
        return dot_view(surface.repos.get_dot(dot_id))

    @app.post("/dots/{dot_id}/messages", status_code=202)
    def message(dot_id: str, body: MessageBody) -> dict[str, Any]:
        stored = post_message(surface.repos, dot_id, body.text, body.profile)
        return {"id": stored.id, "dot_id": stored.dot_id, "profile": stored.profile}

    @app.get("/dots/{dot_id}/thread")
    def thread(dot_id: str) -> dict[str, Any]:
        dot = surface.repos.get_dot(dot_id)
        messages = read_thread(dot, surface.settings, surface.runtime, surface.model)
        return {"dot_id": dot.dot_id, "thread_id": dot.thread_id, "messages": messages}

    @app.get("/dots/{dot_id}/audit")
    def audit_trail(
        dot_id: str,
        request: Request,
        after_id: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        viewer(request, dot_id)
        rows = surface.repos.list_audit(dot_id, after_id=after_id, limit=limit, turn_id=turn_id)
        return {
            "dot_id": dot_id,
            "events": [audit_view(row, surface.runtime.redactor) for row in rows],
            "next_after_id": rows[-1].id if len(rows) == limit else None,
        }

    @app.get("/dots/{dot_id}/sandbox")
    def sandbox_activity(
        dot_id: str,
        request: Request,
        after_id: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        """Audit rows from the pack's sandboxed subagents."""
        dot = viewer(request, dot_id)
        actors = [spec.name for spec in load_pack(REPO_ROOT / "packs" / dot.pack_name).pack.subagents if spec.sandbox]
        rows = surface.repos.list_audit(dot_id, after_id=after_id, limit=limit, actors=actors)
        return {
            "dot_id": dot_id,
            "actors": actors,
            "events": [audit_view(row, surface.runtime.redactor) for row in rows],
            "next_after_id": rows[-1].id if len(rows) == limit else None,
        }

    @app.get("/dots/{dot_id}/jobs")
    def jobs(dot_id: str, request: Request) -> dict[str, Any]:
        viewer(request, dot_id)
        if surface.jobs is None:
            return {"dot_id": dot_id, "jobs": []}
        rows = sorted(surface.jobs.list_jobs(dot_id), key=lambda job: (job.created_at, job.job_id), reverse=True)
        return {"dot_id": dot_id, "jobs": [job_view(job, surface.runtime.redactor) for job in rows]}

    @app.get("/dots/{dot_id}/approvals")
    def approval_cards(dot_id: str, request: Request, status: str | None = None) -> dict[str, Any]:
        viewer(request, dot_id)
        rows = surface.repos.list_dot_approvals(dot_id, status)
        return {"dot_id": dot_id, "approvals": [approval_view(card, surface.runtime.redactor) for card in rows]}

    @app.post("/dots/{dot_id}/corrections", status_code=201)
    def correction(dot_id: str, body: CorrectionBody, request: Request) -> dict[str, Any]:
        """Record "don't do X" against one of the dot's AI messages, as an episode for reflection."""
        dot = viewer(request, dot_id)  # refuses anonymous callers, so the principal is set
        # Building the agent does not invoke it; only its checkpointed history is read.
        graph = build_dot_agent(dot, "chat", settings=surface.settings, runtime=surface.runtime, model=surface.model)
        episode = record_correction(surface.repos, graph, dot, str(principal(request)), body)
        return {"episode_id": episode.id, "dot_id": dot_id}

    @app.get("/dots/{dot_id}/findings")
    def findings(dot_id: str, request: Request, status: str | None = None) -> dict[str, Any]:
        viewer(request, dot_id)
        rows = surface.repos.list_findings(dot_id, status)
        return {"dot_id": dot_id, "findings": [finding_view(row, surface.runtime.redactor) for row in rows]}

    @app.get("/dots/{dot_id}/memory")
    def memory_versions(dot_id: str, request: Request) -> dict[str, Any]:
        viewer(request, dot_id)
        rows = surface.repos.list_memory_versions(dot_id)
        return {"dot_id": dot_id, "versions": [memory_version_view(row, surface.runtime.redactor) for row in rows]}

    @contextmanager
    def dot_lock(dot_id: str) -> Iterator[None]:
        """The worker's per-dot lock, so a memory action never interleaves with a turn or the gate."""
        if not isinstance(surface.repos, PostgresRepositories):
            yield
            return
        locks = DotLocks(surface.repos.pool)
        if not locks.try_acquire(dot_id):
            raise HTTPException(409, "the dot is busy; try again shortly")
        try:
            yield
        finally:
            locks.release(dot_id)

    @app.post("/dots/{dot_id}/memory/{version_id}/{action}")
    def memory_action(
        dot_id: str, version_id: int, action: Literal["rollback", "accept", "discard"], request: Request
    ) -> dict[str, Any]:
        """Approvers roll back an accepted edit, or settle one the gate held for review."""
        user_id = principal(request)
        if user_id is None:
            raise HTTPException(401, "authenticated identity required")
        dot = surface.repos.get_dot(dot_id)
        if user_id not in approvers(dot.pack_name):
            raise HTTPException(403, "only a pack approver may change the dot's memory")
        files = MemoryFiles(surface.runtime.store, dot_id)
        redactor = surface.runtime.redactor
        with dot_lock(dot_id):
            version = surface.repos.get_memory_version(version_id)
            if version.dot_id != dot_id:
                raise NotFound("memory_versions", str(version_id))
            try:
                if action == "rollback":
                    version = rollback(surface.repos, files, version, user_id, redactor)
                elif action == "accept":
                    version = accept_reviewed(surface.repos, files, version, user_id, redactor)
                else:
                    version = discard(surface.repos, version, user_id, redactor)
            except MemoryConflict as exc:
                raise HTTPException(409, str(exc)) from exc
        surface.events.publish(TurnEvent(dot_id, "memory", {"version": version.id, "status": version.status}))
        return memory_version_view(version, redactor)

    @app.post("/schedules/{dot_id}/{name}", status_code=202)
    def schedule_run(dot_id: str, name: str, request: Request) -> dict[str, Any]:
        """Cloud Scheduler's trigger. The body is ignored: profile and prompt come from the pack."""
        if scheduler_verifier is None:
            raise HTTPException(503, "the scheduler webhook is not configured")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token or not scheduler_verifier.allows(token):
            raise HTTPException(401, "a valid scheduler token is required")
        dot = surface.repos.get_dot(dot_id)
        stored = trigger(surface.repos, dot, name, _schedule_time(request.headers.get(_SCHEDULE_TIME_HEADER)))
        return {"dot_id": dot_id, "schedule": name, "queued": stored is not None, "inbox_id": stored and stored.id}

    @app.get("/dots/{dot_id}/events")
    def events_route(dot_id: str, request: Request) -> EventSourceResponse:
        surface.repos.get_dot(dot_id)
        return EventSourceResponse(_events(surface, dot_id, request), ping=_SSE_PING_S)

    return app


def _schedule_time(header: str | None) -> datetime:
    if header:
        try:
            at = datetime.fromisoformat(header)
        except ValueError:
            pass
        else:
            if at.tzinfo is not None:
                return at
    return datetime.now(UTC)


def _job_store(repos: Repositories) -> JobStore | None:
    if isinstance(repos, PostgresRepositories):
        return PostgresJobStore(repos.pool)
    if isinstance(repos, MemoryRepositories):
        return MemoryJobStore(repos)
    return None


def _packs() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for pack_dir in sorted((REPO_ROOT / "packs").iterdir()):
        if not (pack_dir / "pack.yaml").is_file():
            continue
        pack = load_pack(pack_dir).pack
        found.append(
            {"name": pack.name, "profiles": sorted(pack.profiles), "subagents": [s.name for s in pack.subagents]}
        )
    return found


def _events(surface: Surface, dot_id: str, request: Request) -> AsyncIterator[dict[str, str]]:
    if isinstance(surface.events, PgEventChannel):
        url = surface.settings.database_url
        if not url:
            raise RuntimeError("DOT_DATABASE_URL is required to stream events")
        return _pg_events(url, dot_id, request)
    return _memory_events(surface.events, dot_id, request)


async def _memory_events(channel: InMemoryEventChannel, dot_id: str, request: Request) -> AsyncIterator[dict[str, str]]:
    subscription = channel.subscribe()
    while True:
        if await request.is_disconnected():
            return
        try:
            event = await asyncio.to_thread(subscription.get, True, 0.5)
        except queue.Empty:
            continue
        if event.dot_id == dot_id:
            yield _sse(event)


async def _pg_events(database_url: str, dot_id: str, request: Request) -> AsyncIterator[dict[str, str]]:
    conn = await psycopg.AsyncConnection.connect(database_url, autocommit=True)
    try:
        await conn.execute("LISTEN dot_events")
        while True:
            if await request.is_disconnected():
                return
            async for notice in conn.notifies(timeout=1):
                if await request.is_disconnected():
                    return
                try:
                    payload = json.loads(notice.payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict) or payload.get("dot_id") != dot_id:
                    continue
                kind = payload.get("kind")
                detail = payload.get("detail")
                if kind not in get_args(EventKind) or not isinstance(detail, dict):
                    continue
                yield _sse(TurnEvent(dot_id, cast(EventKind, kind), detail))
    finally:
        await conn.close()


def _sse(event: TurnEvent) -> dict[str, str]:
    body = {"dot_id": event.dot_id, "kind": event.kind, "detail": event.detail}
    return {"event": event.kind, "data": json.dumps(body)}


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Uvicorn loads ``dot.surfaces.api:app``. Build it on first use, not at import."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _mount_slack(app: FastAPI, settings: Settings, surface: Surface) -> None:
    """Slack's Events API on GCP. Bolt verifies Slack's request signature, not our identity middleware."""
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    from dot.channels.outbox import MemoryOutbox, PostgresOutbox
    from dot.channels.slack import build_app, web_client
    from dot.persistence.db import MemoryRepositories

    repos = surface.repos
    if isinstance(repos, PostgresRepositories):
        outbox: PostgresOutbox | MemoryOutbox = PostgresOutbox(repos.pool)
    elif isinstance(repos, MemoryRepositories):
        outbox = MemoryOutbox(repos)
    else:
        raise RuntimeError("Slack HTTP mode needs the API's repositories")
    assert settings.slack_bot_token is not None
    bolt = build_app(
        repos,
        outbox,
        client=web_client(settings.slack_bot_token),
        signing_secret=settings.slack_signing_secret,
        redactor=surface.runtime.redactor,
    )
    handler = SlackRequestHandler(bolt)

    @app.post("/slack/events")
    async def slack_events(request: Request) -> Any:
        return await handler.handle(request)
