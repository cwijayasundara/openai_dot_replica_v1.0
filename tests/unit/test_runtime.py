"""Router, turn events and one scripted supervisor turn. No database."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import dot.runtime.worker as worker_module
from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack, seed_store
from dot.persistence.db import Dot, InboxMessage, Repositories, User, open_repositories
from dot.runtime.router import Source, enqueue, render_inbound
from dot.runtime.turns import InMemoryEventChannel, TurnEvent, publish_graph_update
from dot.runtime.worker import run_agent_turn
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

WHEN = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _repos() -> Repositories:
    repos = open_repositories(None)
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("d1", "u1", "research-analyst", "0", "thread-1", "active", WHEN))
    return repos


def test_enqueue_stores_the_message_and_rejects_an_unknown_source() -> None:
    repos = _repos()
    stored = enqueue(repos, "d1", "slack", {"text": "hello"}, "chat")
    assert stored.id > 0
    assert repos.get_inbox(stored.id).payload == {"text": "hello"}
    assert repos.get_inbox(stored.id).source == "slack"
    assert render_inbound(stored) == "[slack] hello"
    with pytest.raises(ValueError, match="unknown inbox source"):
        enqueue(repos, "d1", cast(Source, "sms"), {"text": "no"}, "chat")


def test_graph_updates_become_tool_job_and_interrupt_events() -> None:
    channel = InMemoryEventChannel()
    message = AIMessage(
        content="",
        tool_calls=[{"name": "start_job", "args": {"subagent": "researcher"}, "id": "c1", "type": "tool_call"}],
    )
    publish_graph_update("d1", {"agent": {"messages": [message]}}, channel)
    started = ToolMessage(content='{"ok":true,"job_id":"job_1","status":"queued"}', name="start_job", tool_call_id="c1")
    refused = ToolMessage(content='{"ok":false,"error":"unknown subagent"}', name="start_job", tool_call_id="c2")
    publish_graph_update("d1", {"tools": {"messages": [started, refused]}}, channel)
    reply_to = {"source": "slack", "inbox_id": 4}
    publish_graph_update(
        "d1", {"agent": {"messages": [AIMessage(content="Here is the answer.")]}}, channel, reply_to=reply_to
    )

    class _Interrupt:
        def __init__(self) -> None:
            self.value = {"tool": "send_email"}

    publish_graph_update("d1", {"__interrupt__": (_Interrupt(),)}, channel)
    assert [event.kind for event in channel.events] == ["tool_call", "job_started", "message", "interrupt"]
    assert channel.events[0].detail["name"] == "start_job"
    assert channel.events[1].detail == {"job_id": "job_1", "status": "queued"}
    assert channel.events[2].detail == {"role": "assistant", "text": "Here is the answer.", "channel": reply_to}
    assert channel.events[3].detail == {"value": {"tool": "send_email"}}


def test_an_ai_reply_event_carries_the_message_id() -> None:
    channel = InMemoryEventChannel()
    publish_graph_update("d1", {"agent": {"messages": [AIMessage(content="Hi.", id="ai-7")]}}, channel)
    assert channel.events[0].detail == {"role": "assistant", "text": "Hi.", "message_id": "ai-7"}


def test_subscriber_receives_events_already_published_and_later_ones() -> None:
    channel = InMemoryEventChannel()
    channel.publish(TurnEvent("d1", "message", {"role": "user", "text": "hi"}))
    subscriber = channel.subscribe()
    assert subscriber.get_nowait().detail["text"] == "hi"
    channel.publish(TurnEvent("d1", "error", {"error": "model down"}))
    assert subscriber.get_nowait().detail["error"] == "model down"


class _Search:
    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        del limit
        return [Hit(title="Open Dot", url="https://example.com/dot", snippet=query)]


def test_agent_turn_uses_the_dot_thread(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    runtime = build_graph_runtime(settings)
    dot = Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", WHEN)
    seed_store(load_pack(REPO_ROOT / "packs" / dot.pack_name), runtime.store, dot.dot_id)
    model = ScriptedChatModel(script=[tools(call("web_search", query="open dot")), say("Here is the answer.")])
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), search=_Search())
    events = InMemoryEventChannel()
    batch = [InboxMessage(1, dot.dot_id, "web", {"text": "find q"}, "chat", WHEN)]
    try:
        run_agent_turn(dot, "chat", batch, events, settings=settings, runtime=runtime, model=model, deps=deps)
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, deps=deps, model=model)
        state = agent.get_state({"configurable": {"thread_id": dot.thread_id}})
        other = runtime.checkpointer.get_tuple({"configurable": {"thread_id": "someone-else"}})
    finally:
        runtime.close()

    assert other is None
    messages: list[Any] = state.values["messages"]
    assert any(isinstance(message, HumanMessage) and message.content == "[web] find q" for message in messages)
    assert [event.detail["name"] for event in events.events if event.kind == "tool_call"] == ["web_search"]
    last = events.events[-1]
    assert isinstance(last.detail.pop("message_id"), str)
    assert last == TurnEvent(
        dot.dot_id,
        "message",
        {"role": "assistant", "text": "Here is the answer.", "channel": {"source": "web", "inbox_id": 1}},
    )


def test_a_lane_that_fails_stops_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class StubRuntime:
        def close(self) -> None:
            closed.append(True)

    class FailingWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def run_once(self) -> bool:
            raise RuntimeError("database went away")

    monkeypatch.setattr(worker_module, "build_graph_runtime", lambda settings: StubRuntime())
    monkeypatch.setattr(worker_module, "Worker", FailingWorker)
    monkeypatch.setattr(worker_module, "_events", lambda pool: None)
    monkeypatch.setattr(worker_module, "_agent_runner", lambda settings, runtime: None)
    stop = threading.Event()

    with pytest.raises(RuntimeError, match="database went away"):
        worker_module._serve_lane(Settings(), None, None, None, stop, "learning")  # type: ignore[arg-type]

    assert stop.is_set() and closed == [True]


def test_a_failed_learning_lane_makes_the_worker_exit_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubPool:
        def close(self) -> None:
            pass

    def lane(settings: Any, pool: Any, repos: Any, store: Any, stop: threading.Event, lane: str) -> None:
        if lane == "learning":
            stop.set()
            raise RuntimeError("database went away")
        stop.wait(5)  # the turns lane runs until the failed lane stops it

    monkeypatch.setattr(worker_module, "make_pool", lambda url: StubPool())
    monkeypatch.setattr(worker_module, "migrate", lambda pool: None)
    monkeypatch.setattr(worker_module, "PostgresRepositories", lambda pool: None)
    monkeypatch.setattr(worker_module, "PostgresJobStore", lambda pool: None)
    monkeypatch.setattr(worker_module, "_stop_event", threading.Event)
    monkeypatch.setattr(worker_module, "_serve_lane", lane)
    settings = Settings(_env_file=None, database_url="postgresql://unused", job_workers=0)  # type: ignore[call-arg]

    with pytest.raises(SystemExit) as exited:
        worker_module.serve(settings)

    assert exited.value.code not in (None, 0)
