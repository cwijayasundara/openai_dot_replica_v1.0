"""C1 acceptance offline: one Slack thread carries the message, the job, its result and the approval.

Real Bolt dispatch, the Postgres inbox worker, a job runner and the outbox
delivery loop run together; only Slack's Web API is faked.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from psycopg_pool import ConnectionPool
from slack_bolt import BoltRequest

from dot.assembly import build_graph_runtime
from dot.channels.base import DeliveringEventChannel
from dot.channels.outbox import PostgresOutbox
from dot.channels.slack import SlackDelivery, build_app, run_delivery
from dot.config import Settings
from dot.jobs.runner import JobRunner, run_job
from dot.jobs.store import PostgresJobStore
from dot.packs.loader import REPO_ROOT, load_pack, seed_store
from dot.persistence.db import Dot, InboxMessage, PostgresRepositories, User, make_pool, migrate
from dot.runtime.turns import EventChannel, InMemoryEventChannel
from dot.runtime.worker import Worker, run_agent_turn
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.fake_slack import FakeSlack, authorize, click, dm
from tests.support.job_store_contract import WHEN
from tests.support.outbox_contract import exercise
from tests.support.scripted_model import ScriptedChatModel, call, say, tools
from tests.unit.test_policy_assembly import Credentials, Email

pytestmark = pytest.mark.db
OWNER = "U01OWNER"
WAIT_S = 15


class _Search:
    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        del limit
        return [Hit(title="Open Dot", url="https://example.com/dot", snippet=query)]


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    url = os.environ.get("DOT_DATABASE_URL")
    if not url:
        pytest.skip("DOT_DATABASE_URL is not set")
    opened = make_pool(url)
    migrate(opened)
    with opened.connection() as conn:
        conn.execute(
            "TRUNCATE users, dots, inbox, jobs, approvals, audit_log, findings, episodes,"
            " memory_versions, channel_bindings, outbox, channel_events, approval_posts, message_posts RESTART IDENTITY CASCADE"
        )
    yield opened
    opened.close()


def test_postgres_outbox_matches_the_contract(pool: ConnectionPool) -> None:
    exercise(PostgresOutbox(pool), PostgresRepositories(pool))


def _wait(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_message_job_result_and_approval_share_one_slack_thread(
    pool: ConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = [OWNER]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    repos = PostgresRepositories(pool)
    repos.create_user(User("u1", "Ada", slack_user_id=OWNER))
    repos.create_dot(Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", WHEN))
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    store, outbox, slack = PostgresJobStore(pool), PostgresOutbox(pool), FakeSlack()
    email = Email()
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), search=_Search(), email=email, credentials=Credentials())
    events = DeliveringEventChannel(InMemoryEventChannel(), outbox, ["slack"])

    def check_the_job(messages: list[BaseMessage]) -> AIMessage:
        del messages
        [job] = store.list_jobs("dot-1")
        return tools(call("check_job", job_id=job.job_id))

    supervisor = ScriptedChatModel(
        script=[
            tools(call("start_job", subagent="researcher", instructions="Find open dot runtimes.")),
            say("Started the research."),
            check_the_job,
            tools(call("send_email", to="sam@example.com", subject="Runtimes", body="Two open dot runtimes.")),
            say("Sent the brief to Sam."),
        ]
    )
    researcher = ScriptedChatModel(script=[say("Brief: two open dot runtimes.")])
    dot_runtime, job_runtime = build_graph_runtime(settings), build_graph_runtime(settings)
    for runtime in (dot_runtime, job_runtime):
        runtime.audit_repositories = repos
    dot_runtime.jobs = store
    seed_store(loaded, dot_runtime.store, "dot-1")

    def turn(dot: Dot, profile: str, batch: Sequence[InboxMessage], channel: EventChannel) -> None:
        run_agent_turn(
            dot, profile, batch, channel, settings=settings, runtime=dot_runtime, model=supervisor, deps=deps
        )

    worker = Worker(pool, repos, events, turn)
    runner = JobRunner(
        store,
        repos,
        events,
        lambda dot, job: run_job(dot, job, store, settings=settings, runtime=job_runtime, model=researcher, deps=deps),
        lambda dot_id: deps.artifacts,
    )
    stop = threading.Event()

    def loop(step: Callable[[], Any]) -> None:
        while not stop.is_set():
            if not step():
                stop.wait(0.01)

    threads = [
        threading.Thread(target=loop, args=(worker.run_once,)),
        threading.Thread(target=loop, args=(runner.run_once,)),
        threading.Thread(target=run_delivery, args=(SlackDelivery(outbox, slack), stop, 0.01)),
    ]
    app = build_app(
        repos, outbox, client=slack, slack_signatures_checked=False, process_before_response=True, authorize=authorize
    )
    for thread in threads:
        thread.start()
    try:
        app.dispatch(
            BoltRequest(body=dm(OWNER, "Research open dot runtimes and email Sam.", "500.1"), mode="socket_mode")
        )

        def card() -> dict[str, Any] | None:
            return next((p for p in slack.made("chat.postMessage") if p.get("blocks")), None)

        _wait(lambda: card() is not None, "the approval card in Slack")
        posted = card()
        assert posted is not None
        approval_id = posted["blocks"][-1]["elements"][0]["value"]
        assert email.sent == []

        app.dispatch(BoltRequest(body=click("dot_approve", approval_id, OWNER), mode="socket_mode"))
        _wait(lambda: any(p["text"] == "Sent the brief to Sam." for p in slack.made("chat.postMessage")), "the reply")
        _wait(lambda: len(slack.made("chat.update")) == 1, "the card update")
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=WAIT_S)
        dot_runtime.close()
        job_runtime.close()

    posts = slack.made("chat.postMessage")
    assert [p["text"] for p in posts] == [
        "Started the research.",
        "Approval needed: send_email",
        "Sent the brief to Sam.",
    ]
    assert {(p["channel"], p["thread_ts"]) for p in posts} == {("D1", "500.1")}
    assert email.sent == ["sam@example.com"]
    [update] = slack.made("chat.update")
    assert update["channel"] == "D1" and update["blocks"][-1]["elements"][0]["text"] == f"Approved by <@{OWNER}>"
    assert [job.status for job in store.list_jobs("dot-1")] == ["succeeded"]
    assert repos.get_dot("dot-1").status == "active"
