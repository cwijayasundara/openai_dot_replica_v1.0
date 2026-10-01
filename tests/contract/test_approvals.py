"""HTTP review decisions resume the one assembled graph only in the worker."""

from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import ApprovalConflict, MemoryRepositories
from dot.runtime.router import enqueue
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.safety.approvals import ReviewDecision, decide, persist_interrupts, resume_command
from dot.surfaces.api import create_app
from dot.surfaces.dots import create_dot
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps
from tests.support.coder_task import SCRIPT, SCRIPT_PATH
from tests.support.scripted_model import ScriptedChatModel, call, say, tools
from tests.unit.test_coder_subagent import RecordingSandbox
from tests.unit.test_policy_assembly import Credentials, Email


@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
@pytest.mark.parametrize("count", [1, 2])
def test_persist_authorize_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str, count: int
) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = ["reviewer"]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    repos = MemoryRepositories()
    runtime = build_graph_runtime(settings)
    events = InMemoryEventChannel()
    email = Email()
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), email=email, credentials=Credentials())
    model = ScriptedChatModel(
        script=[
            tools(
                *[call("send_email", to=f"original{n}@example.com", subject="hi", body="hello") for n in range(count)]
            ),
            say("done"),
        ]
    )
    app = create_app(settings, repos=repos, runtime=runtime, events=events, model=model)
    principal: list[str | None] = [None]

    @app.middleware("http")
    async def trusted_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.user_id = principal[0]
        return await call_next(request)

    try:
        dot = create_dot(repos, runtime, "research-analyst", "owner")
        inbound = enqueue(repos, dot.dot_id, "web", {"text": "Send email"}, "chat")
        run_agent_turn(dot, "chat", [inbound], events, settings=settings, runtime=runtime, model=model, deps=deps)
        assert not email.sent and model.calls == 1
        assert repos.get_dot(dot.dot_id).status == "paused"
        cards = list(repos.approvals.values())
        assert len(cards) == count
        assert len([e for e in events.events if e.kind == "approval"]) == count
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model, deps=deps)
        config = {"configurable": {"thread_id": dot.thread_id}}
        persist_interrupts(repos, dot, "chat", agent.get_state(config), events)
        assert len(repos.approvals) == count  # restart/replayed persistence is idempotent
        with TestClient(app) as client:
            endpoint = f"/approvals/{cards[0].approval_id}"
            assert client.post(endpoint, json={"type": decision}, headers={"X-User-ID": "reviewer"}).status_code in {
                401,
                422,
            }
            principal[0] = "outsider"
            assert client.post(endpoint, json={"type": "approve"}).status_code == 403
            assert not repos.episodes and not email.sent
            assert repos.get_dot(dot.dot_id).status == "paused"
            principal[0] = "reviewer"
            for index, card in enumerate(cards):
                body = {"type": decision}
                if decision == "edit":
                    body["edited_args"] = {"to": "edited@example.com", "subject": "hi", "body": "edited"}  # type: ignore[assignment]
                with monkeypatch.context() as failure:

                    def fail_audit(event):
                        raise RuntimeError("audit unavailable")

                    failure.setattr(repos, "append_audit", fail_audit)
                    with pytest.raises(RuntimeError, match="audit unavailable"):
                        client.post(f"/approvals/{card.approval_id}", json=body)
                assert repos.get_approval(card.approval_id).status == "pending"
                assert len(repos.episodes) == index
                assert client.post(f"/approvals/{card.approval_id}", json=body).status_code == 202
                assert len(repos.episodes) == index + 1
                assert not email.sent and model.calls == 1  # HTTP never invokes graph
                assert len([m for m in repos.inbox.values() if m.source == "approval"]) == (
                    1 if index == count - 1 else 0
                )
                assert client.post(f"/approvals/{card.approval_id}", json=body).status_code == 409
        resume = next(m for m in repos.inbox.values() if m.source == "approval")
        run_agent_turn(dot, "chat", [resume], events, settings=settings, runtime=runtime, model=model, deps=deps)
        expected = (
            []
            if decision == "reject"
            else ["edited@example.com"] * count
            if decision == "edit"
            else [f"original{n}@example.com" for n in range(count)]
        )
        assert sorted(email.sent) == sorted(expected)
        assert repos.get_dot(dot.dot_id).status == "active"
        assert len(repos.episodes) == count
        # The resumed run answers the web request that paused it.
        assert events.events[-1].detail == {
            "role": "assistant",
            "text": "done",
            "channel": {"source": "web", "inbox_id": inbound.id},
        }
        with pytest.raises(ApprovalConflict, match="stale"):
            resume_command(repos, dot, resume, agent.get_state(config))
    finally:
        runtime.close()


def test_nested_coder_reviews_pause_again_after_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    repos = MemoryRepositories()
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = ["reviewer"]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    supervisor = ScriptedChatModel(
        script=[tools(call("task", subagent_type="coder", description="Write and execute script")), say("done")]
    )
    heavy = ScriptedChatModel(
        script=[
            tools(call("write_file", file_path=SCRIPT_PATH, content=SCRIPT)),
            tools(call("execute", command=f"python {SCRIPT_PATH}")),
            say("done"),
        ]
    )
    monkeypatch.setattr("dot.assembly.chat_model", lambda role, _: heavy if role == "heavy" else supervisor)
    sandbox = RecordingSandbox()
    monkeypatch.setattr(runtime, "sandbox", lambda *_: sandbox)
    events = InMemoryEventChannel()
    try:
        dot = create_dot(repos, runtime, "research-analyst", "owner")
        row = enqueue(repos, dot.dot_id, "web", {"text": "Write and run a script"}, "chat")
        run_agent_turn(dot, "chat", [row], events, settings=settings, runtime=runtime)
        first = next(iter(repos.approvals.values()))
        assert first.tool == "write_file" and not sandbox.files
        decide(repos, first.approval_id, "reviewer", ReviewDecision(type="approve"))
        resume = max(repos.inbox.values(), key=lambda row: row.id)
        run_agent_turn(dot, "chat", [resume], events, settings=settings, runtime=runtime)
        assert sandbox.files[SCRIPT_PATH] == SCRIPT and not sandbox.commands
        assert repos.get_dot(dot.dot_id).status == "paused"
        second = next(card for card in repos.approvals.values() if card.status == "pending")
        assert second.tool == "execute" and second.run_ref != first.run_ref
        decide(repos, second.approval_id, "reviewer", ReviewDecision(type="approve"))
        resume = max(repos.inbox.values(), key=lambda row: row.id)
        run_agent_turn(dot, "chat", [resume], events, settings=settings, runtime=runtime)
        assert sandbox.commands == [f"python {SCRIPT_PATH}"]
        assert repos.get_dot(dot.dot_id).status == "active"
        assert len(repos.episodes) == 2
        trail = repos.list_audit(dot.dot_id)
        assert {event.actor for event in trail} >= {"supervisor", "coder", "worker", "reviewer"}
        assert len({event.detail["turn_id"] for event in trail}) == 1
        assert all(event.detail["turn_id"] for event in trail)
    finally:
        runtime.close()
