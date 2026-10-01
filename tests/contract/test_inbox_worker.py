"""Inbox worker against Postgres: one supervisor run per dot, dots in parallel."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from dot.persistence.db import Dot, InboxMessage, Repositories, User, make_pool, migrate, open_repositories
from dot.runtime.router import enqueue
from dot.runtime.turns import EventChannel, InMemoryEventChannel, PgEventChannel, TurnEvent
from dot.runtime.worker import Worker

pytestmark = pytest.mark.db


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
    opened = open_repositories(os.environ["DOT_DATABASE_URL"])
    try:
        yield opened
    finally:
        opened.close()


def _seed(repos: Repositories) -> None:
    when = datetime.now(UTC)
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("dot-a", "u1", "research-analyst", "0", "thread-a", "active", when))
    repos.create_dot(Dot("dot-b", "u1", "research-analyst", "0", "thread-b", "active", when))


def _inbox(pool: ConnectionPool, dot_id: str) -> list[dict[str, object]]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT id, payload, profile, claimed_at, done_at, error FROM inbox WHERE dot_id = %s ORDER BY id",
            (dot_id,),
        ).fetchall()
    return list(rows)


def _loop(worker: Worker, stop: threading.Event) -> None:
    while not stop.is_set():
        if not worker.run_once():
            stop.wait(0.01)


def test_messages_during_a_turn_wait_and_two_dots_run_in_parallel(pool: ConnectionPool, repos: Repositories) -> None:
    _seed(repos)
    events = InMemoryEventChannel()
    recorded: list[tuple[str, str, list[str]]] = []
    running = {"dot-a": 0, "dot-b": 0}
    peak = {"dot-a": 0, "dot-b": 0}
    guard = threading.Lock()
    overlap = threading.Event()
    inside_first = threading.Event()
    release_first = threading.Event()
    folded = threading.Event()
    problems: list[str] = []

    def runner(dot: Dot, profile: str, batch: Sequence[InboxMessage], channel: EventChannel) -> None:
        del profile, channel
        texts = [str(message.payload["text"]) for message in batch]
        with guard:
            running[dot.dot_id] += 1
            peak[dot.dot_id] = max(peak[dot.dot_id], running[dot.dot_id])
            if running["dot-a"] and running["dot-b"]:
                overlap.set()
            recorded.append((dot.dot_id, dot.thread_id, list(texts)))
        try:
            if texts == ["first"]:
                inside_first.set()
                if not release_first.wait(5):
                    problems.append("the first turn was never released")
            if texts == ["m1", "m2", "m3"]:
                folded.set()
        finally:
            with guard:
                running[dot.dot_id] -= 1

    worker_a = Worker(pool, repos, events, runner)
    worker_b = Worker(pool, repos, events, runner)
    stop = threading.Event()
    first = threading.Thread(target=_loop, args=(worker_a, stop), name="worker-a")
    second = threading.Thread(target=_loop, args=(worker_b, stop), name="worker-b")
    enqueue(repos, "dot-a", "web", {"text": "first"}, "chat")
    first.start()
    try:
        assert inside_first.wait(5), "dot-a never started"
        enqueue(repos, "dot-b", "web", {"text": "only"}, "chat")
        second.start()
        assert overlap.wait(5), "the two dots never ran at the same time"
        for text in ("m1", "m2", "m3"):
            enqueue(repos, "dot-a", "web", {"text": text}, "chat")
        time.sleep(0.4)
        pending = [row for row in _inbox(pool, "dot-a") if row["payload"]["text"] != "first"]  # type: ignore[index]
        assert [row["payload"]["text"] for row in pending] == ["m1", "m2", "m3"]  # type: ignore[index]
        assert all(row["claimed_at"] is None and row["done_at"] is None for row in pending)
        assert peak["dot-a"] == 1
        release_first.set()
        assert folded.wait(5), "the three messages were not folded into the next turn"
    finally:
        release_first.set()
        stop.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert problems == []
    assert ("dot-a", "thread-a", ["first"]) in recorded
    assert ("dot-b", "thread-b", ["only"]) in recorded
    assert ("dot-a", "thread-a", ["m1", "m2", "m3"]) in recorded
    assert peak == {"dot-a": 1, "dot-b": 1}
    assert all(row["done_at"] is not None and row["error"] is None for row in _inbox(pool, "dot-a"))
    assert all(row["done_at"] is not None and row["error"] is None for row in _inbox(pool, "dot-b"))
    user_texts = [
        event.detail["text"]
        for event in events.events
        if event.kind == "message" and event.dot_id == "dot-a" and event.detail.get("role") == "user"
    ]
    assert user_texts == ["[web] first", "[web] m1", "[web] m2", "[web] m3"]
    assert not first.is_alive()
    assert not second.is_alive()


def test_a_turn_folds_only_the_leading_profile(pool: ConnectionPool, repos: Repositories) -> None:
    _seed(repos)
    seen: list[tuple[str, list[str]]] = []

    def runner(dot: Dot, profile: str, batch: Sequence[InboxMessage], channel: EventChannel) -> None:
        del dot, channel
        seen.append((profile, [str(message.payload["text"]) for message in batch]))

    worker = Worker(pool, repos, InMemoryEventChannel(), runner)
    enqueue(repos, "dot-a", "web", {"text": "a"}, "chat")
    enqueue(repos, "dot-a", "slack", {"text": "b"}, "chat")
    enqueue(repos, "dot-a", "schedule", {"text": "c"}, "sweep")
    assert worker.run_once()
    assert seen == [("chat", ["a", "b"])]
    sweep = [row for row in _inbox(pool, "dot-a") if row["profile"] == "sweep"]
    assert sweep[0]["claimed_at"] is None and sweep[0]["done_at"] is None
    assert worker.run_once()
    assert seen == [("chat", ["a", "b"]), ("sweep", ["c"])]
    assert worker.run_once() is False


def test_a_schedule_row_runs_as_a_turn_of_its_own(pool: ConnectionPool, repos: Repositories) -> None:
    _seed(repos)
    seen: list[tuple[str, list[str]]] = []

    def runner(dot: Dot, profile: str, batch: Sequence[InboxMessage], channel: EventChannel) -> None:
        del dot, channel
        seen.append((profile, [str(message.payload["text"]) for message in batch]))

    worker = Worker(pool, repos, InMemoryEventChannel(), runner)
    enqueue(repos, "dot-a", "schedule", {"text": "s1"}, "sweep")
    enqueue(repos, "dot-a", "web", {"text": "w1"}, "sweep")
    enqueue(repos, "dot-a", "web", {"text": "w2"}, "sweep")
    assert worker.run_once() and worker.run_once()
    # A scheduled run has its own budget and thread, so nothing folds into it.
    assert seen == [("sweep", ["s1"]), ("sweep", ["w1", "w2"])]


def test_a_failed_turn_records_the_error_and_the_dot_continues(pool: ConnectionPool, repos: Repositories) -> None:
    _seed(repos)
    events = InMemoryEventChannel()
    seen: list[str] = []

    def runner(dot: Dot, profile: str, batch: Sequence[InboxMessage], channel: EventChannel) -> None:
        del dot, profile, channel
        text = str(batch[0].payload["text"])
        if text == "boom":
            raise RuntimeError("model down")
        seen.append(text)

    worker = Worker(pool, repos, events, runner)
    enqueue(repos, "dot-a", "web", {"text": "boom"}, "chat")
    assert worker.run_once()
    failed = _inbox(pool, "dot-a")[0]
    assert failed["done_at"] is not None
    assert failed["error"] == "model down"
    assert any(event.kind == "error" and event.detail["error"] == "model down" for event in events.events)
    enqueue(repos, "dot-a", "web", {"text": "next"}, "chat")
    assert worker.run_once()
    assert seen == ["next"]
    assert _inbox(pool, "dot-a")[1]["error"] is None


def test_pg_event_channel_notifies_listeners(pool: ConnectionPool) -> None:
    url = os.environ["DOT_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("LISTEN dot_events")
        PgEventChannel(pool).publish(TurnEvent("dot-a", "error", {"error": "model down"}))
        notice = next(conn.notifies(timeout=2))
    body = json.loads(notice.payload)
    assert body == {"dot_id": "dot-a", "kind": "error", "detail": {"error": "model down"}}
