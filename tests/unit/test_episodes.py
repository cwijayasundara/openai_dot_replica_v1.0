"""L1: episodes point at the state replay re-makes them from; corrections come only from an explicit action."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from dot.assembly import GraphRuntime, build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.memory.episodes import CorrectionBody, ReplayPoint, Unreplayable, record_correction, replay_point
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import Approval, Dot, Episode, MemoryRepositories
from dot.runtime.router import enqueue
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.safety.approvals import ReviewDecision, decide
from dot.surfaces.api import create_app
from dot.surfaces.dots import create_dot, read_thread
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps
from tests.support.scripted_model import ScriptedChatModel, call, say, tools
from tests.unit.test_policy_assembly import Credentials, Email


@dataclass
class Rig:
    settings: Settings
    repos: MemoryRepositories
    runtime: GraphRuntime
    events: InMemoryEventChannel
    model: ScriptedChatModel
    deps: ToolDeps
    email: Email
    dot: Dot

    def turn(self, text: str) -> None:
        inbound = enqueue(self.repos, self.dot.dot_id, "web", {"text": text}, "chat")
        self.run([inbound])

    def resume(self) -> None:
        self.run([next(m for m in self.repos.inbox.values() if m.source == "approval" and m.done_at is None)])

    def run(self, batch: list[Any]) -> None:
        run_agent_turn(
            self.dot,
            "chat",
            batch,
            self.events,
            settings=self.settings,
            runtime=self.runtime,
            model=self.model,
            deps=self.deps,
        )

    def graph(self) -> Any:
        return build_dot_agent(self.dot, "chat", settings=self.settings, runtime=self.runtime, model=self.model)

    def ai_messages(self) -> list[dict[str, Any]]:
        return [m for m in read_thread(self.dot, self.settings, self.runtime, self.model) if m["role"] == "ai"]


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> Rig:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))  # type: ignore[call-arg]
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = ["reviewer"]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    monkeypatch.setattr("dot.surfaces.api.approvers", lambda _: ["reviewer"])
    repos = MemoryRepositories()
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    email = Email()
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), email=email, credentials=Credentials())
    dot = create_dot(repos, runtime, "research-analyst", "owner")
    return Rig(settings, repos, runtime, InMemoryEventChannel(), ScriptedChatModel(script=script), deps, email, dot)


def _email(to: str, body: str = "hello") -> dict[str, Any]:
    return call("send_email", to=to, subject="hi", body=body)


def test_approval_episodes_replay_from_the_state_before_their_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(
        tmp_path,
        monkeypatch,
        [say("Hello."), tools(_email("a@example.com"), _email("b@example.com", "second")), say("done")],
    )
    rig.turn("hi")
    rig.turn("Send both emails")
    cards = sorted(rig.repos.approvals.values(), key=lambda card: card.args["to"])
    decide(rig.repos, cards[0].approval_id, "reviewer", ReviewDecision(type="approve"))
    decide(rig.repos, cards[1].approval_id, "reviewer", ReviewDecision(type="reject", message="not b"))

    graph = rig.graph()
    points = [replay_point(rig.repos, graph, rig.dot, episode) for episode in rig.repos.episodes.values()]
    assert all(isinstance(point, ReplayPoint) for point in points)
    by_target = {point.calls[0]["args"]["to"]: point for point in points if isinstance(point, ReplayPoint)}
    first = by_target["a@example.com"]
    # Each episode judges only its own call, though both came from one model message.
    assert first.calls == [{"name": "send_email", "args": {"to": "a@example.com", "subject": "hi", "body": "hello"}}]
    assert by_target["b@example.com"].calls[0]["args"]["body"] == "second"
    assert first.profile == "chat" and first.thread_id == rig.dot.thread_id
    assert len(first.proposal.tool_calls) == 2
    # Replay restarts after the user's request, without the proposal it re-makes.
    assert isinstance(first.messages[-1], HumanMessage)
    assert "Send both emails" in str(first.messages[-1].content)
    assert [m.content for m in first.messages if isinstance(m, AIMessage)] == ["Hello."]


def test_episodes_a_memory_edit_cannot_change_are_unreplayable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path, monkeypatch, [tools(_email("a@example.com")), say("done")])
    rig.turn("Send it")
    card = next(iter(rig.repos.approvals.values()))
    reference = json.loads(card.run_ref)
    graph = rig.graph()

    def episode_for(approval: Approval) -> Episode:
        rig.repos.approvals[approval.approval_id] = approval
        return rig.repos.insert_episode(
            Episode(
                0,
                rig.dot.dot_id,
                card.decided_at or rig.dot.created_at,
                "task",
                {"approval_id": approval.approval_id},
                "approve",
                {},
            )
        )

    def reason(approval: Approval) -> str:
        point = replay_point(rig.repos, graph, rig.dot, episode_for(approval))
        assert isinstance(point, Unreplayable)
        return point.reason

    job_ref = json.dumps({**reference, "job_id": "job-1"})
    assert reason(replace(card, approval_id="job", run_ref=job_ref)) == "job proposal"
    other_thread = json.dumps({**reference, "thread_id": "elsewhere"})
    assert reason(replace(card, approval_id="other", run_ref=other_thread)) == "not the dot's thread"
    gone = json.dumps({**reference, "checkpoint_id": "1f000000-0000-6000-8000-000000000000"})
    assert reason(replace(card, approval_id="gone", run_ref=gone)) == "checkpoint missing"
    # A nested subagent's call never appears in the supervisor's last message.
    assert (
        reason(replace(card, approval_id="nested", tool="execute", args={"command": "ls"})) == "proposed by a subagent"
    )
    missing = rig.repos.insert_episode(
        Episode(0, rig.dot.dot_id, rig.dot.created_at, "t", {"approval_id": "x"}, "edit", {})
    )
    point = replay_point(rig.repos, graph, rig.dot, missing)
    assert isinstance(point, Unreplayable) and point.reason == "approval card missing"


def test_corrections_are_recorded_only_against_the_dots_own_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, monkeypatch, [say("Here is a long answer."), tools(_email("a@example.com")), say("Sent.")])
    rig.turn("Summarise")
    rig.turn("Email it")
    card = next(iter(rig.repos.approvals.values()))
    decide(rig.repos, card.approval_id, "reviewer", ReviewDecision(type="approve"))
    rig.resume()
    assert rig.email.sent == ["a@example.com"]

    app = create_app(rig.settings, repos=rig.repos, runtime=rig.runtime, events=rig.events, model=rig.model)
    principal: list[str | None] = [None]

    @app.middleware("http")
    async def trusted_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.user_id = principal[0]
        return await call_next(request)

    reply, proposal, sent = rig.ai_messages()
    assert all(m.get("id") for m in (reply, proposal, sent))
    assert proposal["tool_calls"] == ["send_email"]
    endpoint = f"/dots/{rig.dot.dot_id}/corrections"
    before = len(rig.repos.episodes)
    with TestClient(app) as client:
        assert client.post(endpoint, json={"message_id": reply["id"], "text": "Shorter"}).status_code == 401
        principal[0] = "outsider"
        assert client.post(endpoint, json={"message_id": reply["id"], "text": "Shorter"}).status_code == 403
        principal[0] = "owner"
        assert client.post(endpoint, json={"message_id": "nope", "text": "Shorter"}).status_code == 404
        assert client.post(endpoint, json={"message_id": reply["id"], "text": ""}).status_code == 422
        assert len(rig.repos.episodes) == before

        text_only = client.post(endpoint, json={"message_id": reply["id"], "text": "Keep answers short"})
        assert text_only.status_code == 201
        principal[0] = "reviewer"
        on_call = client.post(endpoint, json={"message_id": proposal["id"], "text": "Don't email a@"})
        assert on_call.status_code == 201

    graph = rig.graph()
    text_episode = rig.repos.get_episode(text_only.json()["episode_id"])
    assert text_episode.human_action == "correction"
    assert text_episode.outcome == {"text": "Keep answers short", "by": "owner"}
    assert text_episode.proposal["profile"] == "chat" and text_episode.proposal["tool_calls"] == []
    assert "Summarise" in text_episode.task
    point = replay_point(rig.repos, graph, rig.dot, text_episode)
    assert isinstance(point, Unreplayable) and point.reason == "no tool call"

    call_episode = rig.repos.get_episode(on_call.json()["episode_id"])
    assert call_episode.outcome["by"] == "reviewer"
    point = replay_point(rig.repos, graph, rig.dot, call_episode)
    assert isinstance(point, ReplayPoint)
    assert point.calls == [{"name": "send_email", "args": {"to": "a@example.com", "subject": "hi", "body": "hello"}}]
    # The earliest checkpoint holding the message: before the tool ran.
    assert isinstance(point.messages[-1], HumanMessage) and "Email it" in str(point.messages[-1].content)


def test_corrections_on_runs_without_a_recorded_profile_are_unreplayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, monkeypatch, [tools(call("web_search", query="x")), say("ok")])
    graph = rig.graph()
    # A run before the worker tagged checkpoints with the profile.
    graph.invoke({"messages": [HumanMessage("search")]}, {"configurable": {"thread_id": rig.dot.thread_id}})
    message_id = next(m["id"] for m in rig.ai_messages() if m.get("tool_calls"))
    episode = record_correction(rig.repos, graph, rig.dot, "owner", CorrectionBody(message_id=message_id, text="no"))
    assert episode.proposal["profile"] is None
    point = replay_point(rig.repos, graph, rig.dot, episode)
    assert isinstance(point, Unreplayable) and point.reason == "profile unknown"


def test_a_message_past_the_scan_window_is_too_old_not_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path, monkeypatch, [say("First."), say("Second."), say("Third.")])
    for text in ("one", "two", "three"):
        rig.turn(text)
    first, *_ = rig.ai_messages()
    # Three newest checkpoints all sit in the last turn, so the first message is outside the window.
    monkeypatch.setattr("dot.memory.episodes.CORRECTION_SCAN_LIMIT", 3)
    app = create_app(rig.settings, repos=rig.repos, runtime=rig.runtime, events=rig.events, model=rig.model)

    @app.middleware("http")
    async def trusted_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.user_id = "owner"
        return await call_next(request)

    endpoint = f"/dots/{rig.dot.dot_id}/corrections"
    with TestClient(app) as client:
        old = client.post(endpoint, json={"message_id": first["id"], "text": "Shorter"})
        assert old.status_code == 409 and "too old" in old.json()["detail"]
        assert client.post(endpoint, json={"message_id": "nope", "text": "x"}).status_code == 404
