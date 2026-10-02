"""Inbox consumer. One supervisor turn per dot; other dots run in parallel."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from dot.assembly import (
    GraphRuntime,
    build_dot_agent,
    build_graph_runtime,
    dot_artifacts,
    reflection_model,
    supervisor_model,
)
from dot.channels.base import DeliveringEventChannel
from dot.channels.outbox import PostgresOutbox
from dot.config import Settings, get_settings
from dot.jobs.runner import JobRunner, run_job
from dot.jobs.store import JobStore, PostgresJobStore
from dot.memory.episodes import PROFILE_METADATA_KEY
from dot.memory.reflection import run_reflection
from dot.memory.replay import gate_proposed
from dot.middleware.guardian import GUARDIAN_INSTRUCTION_KEY
from dot.packs.schema import REFLECTION_PROFILE
from dot.persistence.db import Dot, InboxMessage, Json, PostgresRepositories, Repositories, make_pool, migrate
from dot.proactive import runs as scheduled_runs
from dot.proactive.runs import ScheduledRun
from dot.runtime.locks import DotLocks
from dot.runtime.router import CHANNEL_KEY, latest_channel, message_detail, render_inbound
from dot.runtime.turns import EventChannel, PgEventChannel, TurnEvent, publish_graph_update
from dot.safety.approvals import persist_interrupts, resume_command
from dot.tools.native.deps import ToolDeps

log = logging.getLogger(__name__)

# How long to sleep when the inbox has nothing this worker can claim.
_IDLE_WAIT_S = 0.2
_SLACK_REF_KEYS = frozenset({"channel", "thread_ts"})
# Rows that run as a turn of their own: a resume, and a schedule with its own budget.
_ALONE = ("approval", "schedule")

Lane = Literal["turns", "learning"]
# A reflection row: nightly reflection and its replay gate. Each runs on its own lane, so one
# dot's gate (about 100 replay calls) never delays another dot's turn.
_REFLECTION_ROW = "(inbox.source = 'schedule' AND inbox.profile = %s)"

TurnRunner = Callable[[Dot, str, Sequence[InboxMessage], EventChannel], None]


class Worker:
    def __init__(
        self,
        pool: ConnectionPool,
        repos: Repositories,
        events: EventChannel,
        runner: TurnRunner,
        *,
        lane: Lane = "turns",
    ) -> None:
        self._pool = pool
        self._repos = repos
        self._events = events
        self._runner = runner
        self._locks = DotLocks(pool)
        # Built from constants only; the profile is bound as a parameter.
        self._lane_sql = f"AND {'' if lane == 'learning' else 'NOT '}{_REFLECTION_ROW}"

    def run_once(self) -> bool:
        """Claim one dot and run one turn. False when nothing is runnable."""
        skipped: set[str] = set()
        while True:
            dot_id = self._peek(skipped)
            if dot_id is None:
                return False
            # Lock before taking row locks. Two workers that each hold a
            # different inbox row and then wait on the same dot deadlock.
            if not self._locks.try_acquire(dot_id):
                skipped.add(dot_id)
                continue
            try:
                batch = self._claim(dot_id)
                if not batch:
                    continue
                dot = self._repos.get_dot(dot_id)
                self._execute(dot, batch[0].profile, batch)
                return True
            finally:
                self._locks.release(dot_id)

    def _peek(self, skipped: set[str]) -> str | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                f"""
                SELECT dot_id FROM inbox
                WHERE done_at IS NULL AND claimed_at IS NULL
                  AND (source = 'approval' OR NOT EXISTS (
                      SELECT 1 FROM dots WHERE dots.dot_id = inbox.dot_id AND dots.status = 'paused'))
                  {self._lane_sql}
                  AND NOT (dot_id = ANY(%s::text[]))
                ORDER BY created_at, id
                LIMIT 1
                """,
                (REFLECTION_PROFILE, list(skipped)),
            ).fetchone()
        if row is None:
            return None
        return str(row["dot_id"])

    def _claim(self, dot_id: str) -> list[InboxMessage]:
        """Claim the leading same-profile run of pending rows for this dot.

        ``FOR UPDATE SKIP LOCKED`` is the queue claim. Rows inserted after
        this statement stay pending for the next turn.
        """
        with self._pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                f"""
                SELECT id, profile, source FROM inbox
                WHERE dot_id = %s AND done_at IS NULL AND claimed_at IS NULL
                  AND (source = 'approval' OR NOT EXISTS (
                      SELECT 1 FROM dots WHERE dots.dot_id = inbox.dot_id AND dots.status = 'paused'))
                  {self._lane_sql}
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                """,
                (dot_id, REFLECTION_PROFILE),
            ).fetchall()
            ids: list[int] = []
            profile: str | None = None
            for row in rows:
                if ids and (row["source"] in _ALONE or rows[0]["source"] in _ALONE):
                    break
                if profile is None:
                    profile = str(row["profile"])
                if row["profile"] != profile:
                    break
                ids.append(int(row["id"]))
            if not ids:
                return []
            cur.execute("UPDATE inbox SET claimed_at = now() WHERE id = ANY(%s)", (ids,))
        return [self._repos.get_inbox(message_id) for message_id in ids]

    def _execute(self, dot: Dot, profile: str, batch: Sequence[InboxMessage]) -> None:
        for message in batch:
            if message.source != "approval":
                self._events.publish(TurnEvent(dot.dot_id, "message", message_detail(message)))
        try:
            self._runner(dot, profile, batch, self._events)
        except Exception as exc:
            self._events.publish(TurnEvent(dot.dot_id, "error", {"error": str(exc)}))
            self._finish(batch, str(exc))
            return
        self._finish(batch, None)

    def _finish(self, batch: Sequence[InboxMessage], error: str | None) -> None:
        done_at = datetime.now(UTC)
        for message in batch:
            current = self._repos.get_inbox(message.id)
            self._repos.update_inbox(replace(current, done_at=done_at, error=error))


def run_agent_turn(
    dot: Dot,
    profile: str,
    batch: Sequence[InboxMessage],
    events: EventChannel,
    *,
    settings: Settings | None = None,
    runtime: GraphRuntime | None = None,
    model: BaseChatModel | None = None,
    deps: ToolDeps | None = None,
) -> None:
    """Run the assembled supervisor on ``dot.thread_id`` and publish its events.

    A schedule row runs as ``proactive.runs`` decides: a sweep on its own
    thread, a digest only when findings are open.
    """
    settings = settings or get_settings()
    owned = runtime is None
    runtime = runtime or build_graph_runtime(settings)
    scheduled: ScheduledRun | None = None
    try:
        repos = runtime.audit_repositories
        if batch and batch[0].source == "schedule":
            if len(batch) != 1 or repos is None:
                raise ValueError("a scheduled run needs one queue row and repositories")
            schedule = scheduled_runs.schedule_of(dot, batch[0])
            if schedule.kind == "reflection":
                # No agent on the dot's thread: reflection drafts edits, then the gate replays and applies them.
                drafter = reflection_model(settings, model)
                reflection = run_reflection(repos, runtime.store, dot, schedule, drafter, settings, runtime.redactor)
                judged = gate_proposed(repos, runtime, dot, settings, supervisor_model(settings, model))
                if reflection.edits or judged:
                    detail: Json = {"proposed": len(reflection.edits), "judged": [v.id for v in judged]}
                    events.publish(TurnEvent(dot.dot_id, "memory", detail))
                return
            scheduled = scheduled_runs.prepare(repos, dot, batch[0], settings)
            if scheduled is None:
                return
        agent = build_dot_agent(
            dot,
            profile,
            settings=settings,
            runtime=runtime,
            deps=deps,
            model=model,
            scheduled=scheduled,
        )
        thread_id = scheduled.thread_id if scheduled is not None else dot.thread_id
        # Checkpoints carry the profile, so a later correction knows which profile made a message.
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "metadata": {PROFILE_METADATA_KEY: profile},
        }
        snapshot = agent.get_state(config)
        incoming: Any
        if scheduled is not None and repos is not None:
            if snapshot.interrupts:
                raise ValueError("thread is paused for human review")
            incoming = {"messages": [scheduled_runs.request(repos, dot, scheduled, batch[0])]}
        elif batch and batch[0].source == "approval":
            if len(batch) != 1 or repos is None:
                raise ValueError("approval resumes require one queue row and repositories")
            incoming = resume_command(repos, dot, batch[0], snapshot)
        else:
            if snapshot.interrupts:
                raise ValueError("thread is paused for human review")
            incoming = {"messages": [_inbound_message(message) for message in batch]}
        # A resumed approval answers the request that paused the thread.
        tagged = incoming["messages"] if isinstance(incoming, dict) else snapshot.values.get("messages", [])
        reply_to = latest_channel(tagged)
        if scheduled is not None and scheduled.schedule.kind == "sweep":
            events = scheduled_runs.Silent(events)
        for chunk in agent.stream(incoming, config, stream_mode="updates"):
            if isinstance(chunk, dict):
                publish_graph_update(dot.dot_id, chunk, events, reply_to=reply_to)
        snapshot = agent.get_state(config)
        if scheduled is not None and repos is not None:
            if snapshot.interrupts and scheduled.thread_id != dot.thread_id:
                raise ValueError(f"sweep {scheduled.schedule.name!r} cannot wait for approval")
            scheduled_runs.finish(repos, dot, scheduled, snapshot.values.get("messages", []))
        if repos is not None:
            persist_interrupts(repos, dot, profile, snapshot, events, redactor=runtime.redactor)
        elif snapshot.interrupts:
            raise ValueError("persisting approvals requires repositories")
    finally:
        try:
            # A sweep's thread is scratch: its findings and audit rows are the record.
            if scheduled is not None and scheduled.thread_id != dot.thread_id:
                runtime.checkpointer.delete_thread(scheduled.thread_id)
        finally:
            if owned:
                runtime.close()


def _inbound_message(message: InboxMessage) -> HumanMessage:
    channel: Json = {"source": message.source, "inbox_id": message.id}
    reply_ref = message.payload.get("reply_ref")
    # Only a channel adapter writes reply_ref; it addresses that channel's conversation.
    if message.source == "slack" and isinstance(reply_ref, dict):
        channel["reply_ref"] = {key: str(value) for key, value in reply_ref.items() if key in _SLACK_REF_KEYS}
    tags: Json = {}
    if message.source == "job_result":
        # A job result is not a user request: the Guardian reviews what the dot
        # does with it against the instruction that started the job, and the
        # reply belongs where that request came from.
        origin = message.payload.get("origin")
        origin = origin if isinstance(origin, dict) else {}
        instruction = origin.get("instruction")
        tags[GUARDIAN_INSTRUCTION_KEY] = instruction if isinstance(instruction, str) else ""
        if isinstance(origin.get("channel"), dict):
            channel = dict(origin["channel"])
    tags[CHANNEL_KEY] = channel
    return HumanMessage(content=render_inbound(message), additional_kwargs=tags)


def _agent_runner(settings: Settings, runtime: GraphRuntime) -> TurnRunner:
    def runner(dot: Dot, profile: str, batch: Sequence[InboxMessage], events: EventChannel) -> None:
        run_agent_turn(dot, profile, batch, events, settings=settings, runtime=runtime)

    return runner


def serve(settings: Settings | None = None) -> None:
    """Poll the inbox until SIGINT or SIGTERM."""
    settings = settings or get_settings()
    url = settings.database_url
    if not url:
        raise SystemExit("DOT_DATABASE_URL is required for the worker")
    pool = make_pool(url)
    try:
        migrate(pool)
        repos = PostgresRepositories(pool)
        stop = _stop_event()
        store = PostgresJobStore(pool)
        job_threads = [
            threading.Thread(
                target=_serve_jobs, args=(settings, pool, repos, store, stop), name=f"job-runner-{n}", daemon=True
            )
            for n in range(settings.job_workers)
        ]
        for thread in job_threads:
            thread.start()
        learning_failed = threading.Event()

        def serve_learning() -> None:
            try:
                _serve_lane(settings, pool, repos, store, stop, "learning")
            except Exception:
                learning_failed.set()  # _serve_lane has logged it and stopped the turns lane

        learning = threading.Thread(target=serve_learning, name="learning", daemon=True)
        learning.start()
        try:
            _serve_lane(settings, pool, repos, store, stop, "turns")
        finally:
            stop.set()
            learning.join()
            for thread in job_threads:
                thread.join()
        # Exit non-zero, so a supervisor that restarts only on failure brings the learning lane back.
        if learning_failed.is_set():
            raise SystemExit("the learning lane failed")
    finally:
        pool.close()


def _serve_lane(
    settings: Settings, pool: ConnectionPool, repos: Repositories, store: JobStore, stop: threading.Event, lane: Lane
) -> None:
    """One inbox loop. The graph runtime and the lock table are per thread; neither is thread-safe."""
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    runtime.jobs = store
    try:
        worker = Worker(pool, repos, _events(pool), _agent_runner(settings, runtime), lane=lane)
        while not stop.is_set():
            if not worker.run_once():
                stop.wait(_IDLE_WAIT_S)
    except Exception:
        # A lane that dies alone leaves the worker half alive; stop the other lane so a supervisor restarts it.
        log.exception("the %s lane failed", lane)
        stop.set()
        raise
    finally:
        runtime.close()


def _events(pool: ConnectionPool) -> EventChannel:
    """Live events, plus outbox rows for replies and cards that belong to Slack."""
    return DeliveringEventChannel(PgEventChannel(pool), PostgresOutbox(pool), ["slack"])


def _serve_jobs(
    settings: Settings, pool: ConnectionPool, repos: Repositories, store: JobStore, stop: threading.Event
) -> None:
    """One job at a time on this thread. The graph runtime is not shared across threads."""
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    try:
        runner = JobRunner(
            store,
            repos,
            _events(pool),
            lambda dot, job: run_job(dot, job, store, settings=settings, runtime=runtime),
            lambda dot_id: dot_artifacts(settings, dot_id),
            runtime.redactor,
        )
        while not stop.is_set():
            if not runner.run_once():
                stop.wait(_IDLE_WAIT_S)
    finally:
        runtime.close()


def _stop_event() -> threading.Event:
    stop = threading.Event()

    def handle(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    return stop


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
