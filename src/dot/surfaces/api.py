"""HTTP surface. Routes write the inbox and stream events. They never run an agent."""

from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, cast

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from dot.assembly import GraphRuntime, build_graph_runtime
from dot.config import Settings, get_settings
from dot.packs.loader import PackLoadError
from dot.persistence.db import ApprovalConflict, NotFound, PostgresRepositories, Repositories, open_repositories
from dot.runtime.turns import EventKind, InMemoryEventChannel, PgEventChannel, TurnEvent
from dot.safety.approvals import ReviewDecision, approvers, decide
from dot.surfaces.dots import create_dot, dot_view, post_message, read_thread

_SSE_PING_S = 0


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
        *,
        model: BaseChatModel | None = None,
        owns_repos: bool = False,
        owns_runtime: bool = False,
    ) -> None:
        self.settings = settings
        self.repos = repos
        self.runtime = runtime
        self.events = events
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

    @app.post("/approvals/{approval_id}", status_code=202)
    def approval_decision(approval_id: str, body: ReviewDecision, request: Request) -> dict[str, Any]:
        # Only trusted authentication middleware may set this principal.
        user_id = getattr(request.state, "user_id", None)
        if not isinstance(user_id, str) or not user_id:
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
        user_id = getattr(request.state, "user_id", None)
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(401, "authenticated identity required")
        dot = surface.repos.get_dot(dot_id)
        if user_id != dot.owner_user_id and user_id not in approvers(dot.pack_name):
            raise HTTPException(403, "audit access is not permitted")
        rows = surface.repos.list_audit(dot_id, after_id=after_id, limit=limit, turn_id=turn_id)
        return {
            "dot_id": dot_id,
            "events": [surface.runtime.redactor.content(asdict(row)) for row in rows],
            "next_after_id": rows[-1].id if len(rows) == limit else None,
        }

    @app.get("/dots/{dot_id}/events")
    def events_route(dot_id: str, request: Request) -> EventSourceResponse:
        surface.repos.get_dot(dot_id)
        return EventSourceResponse(_events(surface, dot_id, request), ping=_SSE_PING_S)

    return app


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
                known = {"message", "tool_call", "interrupt", "approval", "job_started", "job_finished", "error"}
                if kind not in known or not isinstance(detail, dict):
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
