"""J2 acceptance: the inbox worker and a job runner side by side against Postgres."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from psycopg_pool import ConnectionPool

from dot.assembly import GraphRuntime, build_graph_runtime
from dot.config import Settings
from dot.jobs.runner import JobRunner, run_job
from dot.jobs.store import PostgresJobStore
from dot.packs.loader import REPO_ROOT, load_pack, seed_store
from dot.persistence.db import Dot, InboxMessage, PostgresRepositories, User, make_pool, migrate
from dot.runtime.router import enqueue
from dot.runtime.turns import EventChannel, InMemoryEventChannel, TurnEvent
from dot.runtime.worker import Worker, run_agent_turn
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.job_store_contract import WHEN
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

pytestmark = pytest.mark.db
WAIT_S = 10


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        del limit
        self.queries.append(query)
        return [Hit(title="Open Dot", url="https://example.com/dot", snippet=query)]


@dataclass
class Rig:
    pool: ConnectionPool
    repos: PostgresRepositories
    store: PostgresJobStore
    settings: Settings
    events: InMemoryEventChannel
    deps: ToolDeps
    stop: threading.Event
    job_ran: threading.Event

    def start(self, supervisor: ScriptedChatModel, job_model: ScriptedChatModel) -> list[threading.Thread]:
        dot_runtime = self._runtime()
        dot_runtime.jobs = self.store
        seed_store(load_pack(REPO_ROOT / "packs/research-analyst"), dot_runtime.store, "dot-1")
        job_runtime = self._runtime()

        def turn(dot: Dot, profile: str, batch: Sequence[InboxMessage], channel: EventChannel) -> None:
            run_agent_turn(
                dot,
                profile,
                batch,
                channel,
                settings=self.settings,
                runtime=dot_runtime,
                model=supervisor,
                deps=self.deps,
            )

        worker = Worker(self.pool, self.repos, self.events, turn)
        runner = JobRunner(
            self.store,
            self.repos,
            self.events,
            lambda dot, job: run_job(
                dot, job, self.store, settings=self.settings, runtime=job_runtime, model=job_model, deps=self.deps
            ),
            lambda dot_id: self.deps.artifacts,
        )
        threads = [
            threading.Thread(target=self._loop, args=(worker.run_once, dot_runtime, None), name="inbox"),
            threading.Thread(target=self._loop, args=(runner.run_once, job_runtime, self.job_ran), name="jobs"),
        ]
        for thread in threads:
            thread.start()
        return threads

    def _runtime(self) -> GraphRuntime:
        runtime = build_graph_runtime(self.settings)
        runtime.audit_repositories = self.repos
        return runtime

    def _loop(self, step: Callable[[], bool], runtime: GraphRuntime, ran: threading.Event | None) -> None:
        try:
            while not self.stop.is_set():
                if step():
                    if ran is not None:
                        ran.set()
                else:
                    self.stop.wait(0.01)
        finally:
            runtime.close()

    def said(self, text: str) -> threading.Event:
        """Set once the dot says ``text``; checks events already published too."""
        seen = threading.Event()
        subscriber = self.events.subscribe()

        def watch() -> None:
            while not self.stop.is_set():
                try:
                    event: TurnEvent = subscriber.get(timeout=0.05)
                except Exception:
                    continue
                if event.kind == "message" and event.detail.get("text") == text:
                    seen.set()
                    return

        threading.Thread(target=watch, daemon=True).start()
        return seen

    def jobs(self) -> list[str]:
        return [job.status for job in self.store.list_jobs("dot-1")]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    url = os.environ.get("DOT_DATABASE_URL")
    if not url:
        pytest.skip("DOT_DATABASE_URL is not set")
    pool = make_pool(url)
    migrate(pool)
    with pool.connection() as conn:
        conn.execute(
            "TRUNCATE users, dots, inbox, jobs, approvals, audit_log, findings, episodes,"
            " memory_versions, channel_bindings RESTART IDENTITY CASCADE"
        )
    repos = PostgresRepositories(pool)
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", WHEN))
    # Graph state in memory; the queues, jobs and locks are Postgres.
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), search=_Search())
    stop = threading.Event()
    yield Rig(pool, repos, PostgresJobStore(pool), settings, InMemoryEventChannel(), deps, stop, threading.Event())
    stop.set()
    pool.close()


def _job_id(rig: Rig) -> Callable[[list[BaseMessage]], AIMessage]:
    def step(messages: list[BaseMessage]) -> AIMessage:
        del messages
        [job] = rig.store.list_jobs("dot-1")
        return tools(call("check_job", job_id=job.job_id))

    return step


def _join(rig: Rig, threads: list[threading.Thread]) -> None:
    rig.stop.set()
    for thread in threads:
        thread.join(timeout=WAIT_S)
        assert not thread.is_alive()


def test_the_dot_answers_while_a_job_runs_then_reports_where_it_was_asked(rig: Rig) -> None:
    job_started = threading.Event()
    finish_job = threading.Event()

    def research(messages: list[BaseMessage]) -> AIMessage:
        del messages
        job_started.set()
        assert finish_job.wait(WAIT_S), "the job was never released"
        return say("Brief: two open dot runtimes exist.")

    supervisor = ScriptedChatModel(
        script=[
            tools(call("start_job", subagent="researcher", instructions="Find open dot runtimes.")),
            say("Started the research."),
            say("It is Tuesday."),
            _job_id(rig),
            say("The brief is ready: two open dot runtimes exist."),
        ]
    )
    threads = rig.start(supervisor, ScriptedChatModel(script=[research]))
    try:
        ask = enqueue(rig.repos, "dot-1", "slack", {"text": "Research open dot runtimes."}, "chat")
        assert job_started.wait(WAIT_S), "the job never started"
        answered = rig.said("It is Tuesday.")
        question = enqueue(rig.repos, "dot-1", "web", {"text": "What day is it?"}, "chat")
        assert answered.wait(WAIT_S), "the dot did not answer while the job ran"
        assert rig.jobs() == ["running"]

        reported = rig.said("The brief is ready: two open dot runtimes exist.")
        finish_job.set()
        assert reported.wait(WAIT_S), "the dot never reported the job result"
    finally:
        finish_job.set()
        _join(rig, threads)

    assert rig.jobs() == ["succeeded"]
    [job] = rig.store.list_jobs("dot-1")
    assert job.origin["channel"] == {"source": "slack", "inbox_id": ask.id}
    with rig.pool.connection() as conn:
        rows = conn.execute("SELECT source, profile, payload, done_at FROM inbox ORDER BY id").fetchall()
    assert [(source, profile) for source, profile, _, _ in rows] == [
        ("slack", "chat"),
        ("web", "chat"),
        ("job_result", "chat"),
    ]
    assert all(done is not None for *_, done in rows)
    assert rows[2][2]["origin"]["channel"] == {"source": "slack", "inbox_id": ask.id}
    slack = {"source": "slack", "inbox_id": ask.id}
    replies = [
        (e.detail["text"], e.detail["channel"])
        for e in rig.events.events
        if e.kind == "message" and e.detail["role"] == "assistant"
    ]
    assert replies == [
        ("Started the research.", slack),
        ("It is Tuesday.", {"source": "web", "inbox_id": question.id}),
        ("The brief is ready: two open dot runtimes exist.", slack),
    ]
    assert [e.detail for e in rig.events.events if e.kind == "job_started"] == [
        {"job_id": job.job_id, "status": "queued"}
    ]


def test_cancel_stops_a_running_job_within_one_step(rig: Rig) -> None:
    job_started = threading.Event()
    cancelled = threading.Event()

    def research(messages: list[BaseMessage]) -> AIMessage:
        del messages
        job_started.set()
        assert cancelled.wait(WAIT_S), "the cancel turn never ran"
        return tools(call("web_search", query="open dot"))

    def cancel(messages: list[BaseMessage]) -> AIMessage:
        del messages
        [job] = rig.store.list_jobs("dot-1")
        return tools(call("cancel_job", job_id=job.job_id))

    supervisor = ScriptedChatModel(
        script=[
            tools(call("start_job", subagent="researcher", instructions="Find open dot runtimes.")),
            say("Started."),
            cancel,
            say("Cancelled."),
        ]
    )
    job_model = ScriptedChatModel(script=[research, say("never reached")])
    threads = rig.start(supervisor, job_model)
    try:
        enqueue(rig.repos, "dot-1", "slack", {"text": "Research open dot runtimes."}, "chat")
        assert job_started.wait(WAIT_S), "the job never started"
        done = rig.said("Cancelled.")
        enqueue(rig.repos, "dot-1", "slack", {"text": "Actually, stop that."}, "chat")
        assert done.wait(WAIT_S)
        assert rig.jobs() == ["cancelled"]
        cancelled.set()
        assert rig.job_ran.wait(WAIT_S), "the job run never returned"
    finally:
        cancelled.set()
        _join(rig, threads)

    assert job_model.calls == 1
    assert rig.deps.search is not None and rig.deps.search.queries == []  # type: ignore[attr-defined]
    assert rig.jobs() == ["cancelled"]
    with rig.pool.connection() as conn:
        sources = [row[0] for row in conn.execute("SELECT source FROM inbox ORDER BY id").fetchall()]
    assert sources == ["slack", "slack"]  # no job_result for a cancelled job
    assert not any(e.kind == "job_finished" for e in rig.events.events)
