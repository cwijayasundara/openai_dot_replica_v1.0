"""API plus worker, scripted model, Postgres. The API enqueues; the worker answers."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from dot.assembly import build_graph_runtime
from dot.config import Settings
from dot.memory.reflection import AGENTS_PATH, MemoryFiles
from dot.persistence.db import (
    MemoryVersion,
    PostgresRepositories,
    Repositories,
    make_pool,
    migrate,
    open_repositories,
)
from dot.runtime.locks import DotLocks
from dot.runtime.turns import PgEventChannel, TurnEvent
from dot.runtime.worker import Worker, run_agent_turn
from dot.surfaces.api import create_app
from dot.surfaces.cli import iter_tail
from dot.surfaces.dots import create_dot
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

pytestmark = pytest.mark.db


class _Search:
    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        del limit
        return [Hit(title="Hello", url="https://example.com/hello", snippet=query)]


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    url = os.environ.get("DOT_DATABASE_URL")
    if not url:
        pytest.skip("DOT_DATABASE_URL is not set")
    opened = make_pool(url)
    try:
        migrate(opened)
        with opened.connection() as conn:
            conn.execute(
                "TRUNCATE users, dots, inbox, jobs, approvals, audit_log, findings, episodes,"
                " memory_versions, channel_bindings RESTART IDENTITY CASCADE"
            )
    except Exception:
        opened.close()
        raise
    yield opened
    opened.close()


@pytest.fixture
def repos(pool: ConnectionPool) -> Iterator[Repositories]:
    del pool
    opened = open_repositories(os.environ["DOT_DATABASE_URL"])
    try:
        yield opened
    finally:
        opened.close()


def test_a_queued_message_is_answered_on_the_thread_and_the_event_stream(
    tmp_path: Path, pool: ConnectionPool, repos: Repositories
) -> None:
    del pool
    assert isinstance(repos, PostgresRepositories)
    url = os.environ["DOT_DATABASE_URL"]
    settings = Settings(_env_file=None, database_url=url, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    runtime = build_graph_runtime(settings)
    model = ScriptedChatModel(
        script=[
            tools(call("web_search", query="hello")),
            say("Hello from the dot."),
            say("Hello from the dot."),
        ]
    )
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), search=_Search())
    app = create_app(settings, repos=repos, runtime=runtime, model=model)

    def runner(dot: object, profile: str, batch: object, events: object) -> None:
        run_agent_turn(
            dot,  # type: ignore[arg-type]
            profile,
            batch,  # type: ignore[arg-type]
            events,  # type: ignore[arg-type]
            settings=settings,
            runtime=runtime,
            model=model,
            deps=deps,
        )

    worker = Worker(repos.pool, repos, PgEventChannel(repos.pool), runner)
    try:
        asyncio.run(_round_trip(app, worker))
    finally:
        runtime.close()


async def _round_trip(app: object, worker: Worker) -> None:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/dots",
            json={"pack": "research-analyst", "owner_user_id": "ada", "display_name": "Ada"},
        )
        assert created.status_code == 201
        dot_id = created.json()["dot_id"]
        queued = await client.post(f"/dots/{dot_id}/messages", json={"text": "hello"})
        assert queued.status_code == 202

        body = await _read_sse_until(app, f"/dots/{dot_id}/events", worker, "Hello from the dot.")
        thread = await client.get(f"/dots/{dot_id}/thread")
        assert thread.status_code == 200
        messages = thread.json()["messages"]

    contents = [message["content"] for message in messages]
    assert "[web] hello" in contents
    assert "Hello from the dot." in contents
    assert any(message.get("tool_calls") == ["web_search"] for message in messages)
    data = [json.loads(line.removeprefix("data:").strip()) for line in body.splitlines() if line.startswith("data:")]
    assert any(item["detail"].get("text") == "Hello from the dot." for item in data)
    assert any(item["kind"] == "tool_call" and item["detail"].get("name") == "web_search" for item in data)


async def _read_sse_until(app: object, path: str, worker: Worker, needle: str) -> str:
    """Drive the ASGI app until ``needle`` is streamed. httpx's ASGI transport buffers the body."""
    chunks: list[bytes] = []
    found = asyncio.Event()
    disconnected = asyncio.Event()
    request_sent = False
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "state": {},
    }

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message.get("type") != "http.response.body":
            return
        body = message.get("body", b"")
        if isinstance(body, bytes) and body:
            chunks.append(body)
            if needle.encode() in b"".join(chunks):
                found.set()

    async def serve() -> None:
        await app(scope, receive, send)  # type: ignore[operator]

    task = asyncio.create_task(serve())
    try:
        await asyncio.sleep(0.3)
        assert await asyncio.to_thread(worker.run_once)
        await asyncio.wait_for(found.wait(), timeout=10)
    finally:
        disconnected.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return b"".join(chunks).decode()


def test_tail_prints_a_live_event(pool: ConnectionPool) -> None:
    url = os.environ["DOT_DATABASE_URL"]
    event = TurnEvent("dot-a", "message", {"role": "assistant", "text": "tail me"})

    def publish() -> None:
        PgEventChannel(pool).publish(event)

    def on_listen() -> None:
        threading.Thread(target=publish, daemon=True).start()

    payloads = list(iter_tail(url, "dot-a", timeout_s=2, on_listen=on_listen))
    assert payloads
    assert payloads[0]["detail"]["text"] == "tail me"


def test_memory_actions_wait_for_the_dots_lock(
    tmp_path: Path, pool: ConnectionPool, repos: Repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dot.surfaces.api.approvers", lambda _: ["reviewer"])
    url = os.environ["DOT_DATABASE_URL"]
    settings = Settings(_env_file=None, database_url=url, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    runtime = build_graph_runtime(settings)
    dot = create_dot(repos, runtime, "research-analyst", "owner")
    detail = {"path": AGENTS_PATH, "find": "", "replace": "- Keep emails short.\n", "rationale": "r"}
    version = repos.insert_memory_version(
        MemoryVersion(0, dot.dot_id, datetime.now(UTC), "", [], "needs_review", detail)
    )
    app = create_app(settings, repos=repos, runtime=runtime, model=ScriptedChatModel(script=[]))

    @app.middleware("http")
    async def trusted_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.user_id = "reviewer"
        return await call_next(request)

    worker_locks = DotLocks(pool)
    assert worker_locks.try_acquire(dot.dot_id)  # a turn is running
    with TestClient(app) as client:
        busy = client.post(f"/dots/{dot.dot_id}/memory/{version.id}/accept")
        assert busy.status_code == 409 and "busy" in busy.json()["detail"]
        assert repos.get_memory_version(version.id).status == "needs_review"
        worker_locks.release(dot.dot_id)
        assert client.post(f"/dots/{dot.dot_id}/memory/{version.id}/accept").json()["status"] == "accepted"
    assert MemoryFiles(runtime.store, dot.dot_id).read(AGENTS_PATH) == "- Keep emails short.\n"
    assert [e.decision for e in repos.list_audit(dot.dot_id) if e.kind == "memory"] == ["accept"]
    runtime.close()
