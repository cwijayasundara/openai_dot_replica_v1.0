"""A complete turn audit and secret-free model requests through the one assembly."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Decision
from dot.persistence.db import AppendOnly, MemoryRepositories
from dot.runtime.router import enqueue
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.safety.credentials import CredentialBroker, EnvironmentSecrets
from dot.surfaces.api import create_app
from dot.surfaces.dots import create_dot
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.scripted_model import ScriptedChatModel, call, say, tools


@pytest.mark.parametrize("transport_error", [False, True])
def test_full_trail_and_no_secrets_in_any_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport_error: bool
) -> None:
    configured = uuid4().hex
    resolved = uuid4().hex
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path), slack_bot_token=configured)
    repos = MemoryRepositories()
    runtime = build_graph_runtime(settings)
    events = InMemoryEventChannel()
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = ["reviewer"]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    broker = CredentialBroker({"cred:smtp": "SMTP_TEST"}, EnvironmentSecrets({"SMTP_TEST": resolved}), runtime.redactor)
    reached = []

    class LeakyEmail:
        def send(self, *, to, subject, body, credential):
            assert credential == resolved
            reached.append(to)
            if transport_error:
                raise RuntimeError(f"provider returned {credential}")
            return f"provider echoed {credential}"

    class LeakySearch:
        def search(self, query, *, limit=5):
            return [Hit("result", "https://example.test", f"{resolved} {configured}")]

    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), credentials=broker, email=LeakyEmail(), search=LeakySearch())
    first = tools(call("send_email", to="original@example.com", subject="hi", body="hello"))
    first.additional_kwargs["reasoning_content"] = configured
    model = ScriptedChatModel(script=[first, tools(call("web_search", query="check status")), say("done")])
    app = create_app(settings, repos=repos, runtime=runtime, events=events, model=model)
    principal = ["owner"]

    @app.middleware("http")
    async def trusted_identity(request: Request, call_next):
        request.state.user_id = principal[0]
        return await call_next(request)

    try:
        dot = create_dot(repos, runtime, "research-analyst", "owner")
        other = create_dot(repos, runtime, "research-analyst", "owner")
        inbound = enqueue(repos, dot.dot_id, "web", {"text": f"Send email; {configured}"}, "chat")
        run_agent_turn(dot, "chat", [inbound], events, settings=settings, runtime=runtime, model=model, deps=deps)
        card = next(iter(repos.approvals.values()))
        before = repos.list_audit(dot.dot_id)
        assert [row.kind for row in before] == ["tool_call", "policy", "guardian", "approval"]
        turn_id = before[0].detail["turn_id"]
        assert turn_id
        with TestClient(app) as client:
            endpoint = f"/dots/{dot.dot_id}/audit"
            principal[0] = None
            assert client.get(endpoint).status_code == 401
            principal[0] = "outsider"
            assert client.get(endpoint).status_code == 403
            principal[0] = "reviewer"
            assert (
                client.post(
                    f"/approvals/{card.approval_id}",
                    json={
                        "type": "edit",
                        "edited_args": {"to": "edited@example.com", "subject": "hi", "body": "edited"},
                    },
                ).status_code
                == 202
            )
            resume = next(row for row in repos.inbox.values() if row.source == "approval")
            run_agent_turn(dot, "chat", [resume], events, settings=settings, runtime=runtime, model=model, deps=deps)
            trail = client.get(endpoint, params={"turn_id": turn_id}).json()["events"]
            expected = repos.list_audit(dot.dot_id, turn_id=turn_id)
            assert [row["id"] for row in trail] == [row.id for row in expected]
            assert all(row["detail"]["turn_id"] == turn_id for row in trail)
            assert [row["decision"] for row in trail if row["kind"] == "approval"] == ["pending", "edit"]
            reviews = [row for row in trail if row["kind"] == "guardian"]
            assert [row["detail"]["args"]["to"] for row in reviews] == ["original@example.com", "edited@example.com"]
            assert any(row["detail"].get("phase") == "result" for row in trail if row["tool"] == "send_email")
            collected = []
            after = 0
            while True:
                page = client.get(endpoint, params={"limit": 2, "after_id": after}).json()
                collected.extend(page["events"])
                if page["next_after_id"] is None:
                    break
                after = page["next_after_id"]
            assert collected == trail
            assert client.get(f"/dots/{other.dot_id}/audit").json()["events"] == []
            assert client.get(endpoint, params={"turn_id": "unknown"}).json()["events"] == []
            assert client.get(endpoint, params={"limit": 1001}).status_code == 422
        assert reached == ["edited@example.com"]
        for secret in (configured, resolved):
            for request in [*model.seen, *model.structured_seen]:
                assert secret not in json.dumps([message.model_dump() for message in request])
            assert secret not in json.dumps(trail)
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model, deps=deps)
        snapshot = agent.get_state({"configurable": {"thread_id": dot.thread_id}})
        assert resolved not in json.dumps([message.model_dump() for message in snapshot.values["messages"]])
        with pytest.raises(AppendOnly):
            repos.update_audit(expected[0])
        returned = repos.get_audit(expected[0].id)
        returned.detail["phase"] = "tampered"
        assert repos.get_audit(expected[0].id).detail["phase"] == "proposed"
    finally:
        runtime.close()


@pytest.mark.parametrize("blocked_by", ["policy", "guardian", "surface"])
def test_refusals_are_audited_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_by: str
) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    runtime = build_graph_runtime(settings)
    repos = MemoryRepositories()
    runtime.audit_repositories = repos
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    if blocked_by == "policy":
        loaded.policy.tools["send_email"] = Decision.block
    monkeypatch.setattr("dot.assembly.load_pack", lambda _: loaded)
    name = "execute" if blocked_by == "surface" else "send_email"
    args = {"command": "echo no"} if name == "execute" else {"to": "x@example.com", "subject": "hi", "body": "hello"}
    model = ScriptedChatModel(
        script=[tools(call(name, **args)), say("done")],
        structured_script=[{"in_scope": False, "risk": "high", "reason": "refused"}],
    )
    try:
        dot = create_dot(repos, runtime, "research-analyst", "owner")
        row = enqueue(repos, dot.dot_id, "web", {"text": "do it"}, "chat")
        run_agent_turn(dot, "chat", [row], InMemoryEventChannel(), settings=settings, runtime=runtime, model=model)
        audit = repos.list_audit(dot.dot_id)
        assert audit[0].kind == "tool_call"
        assert any(row.kind == "policy" for row in audit)
        if blocked_by == "guardian":
            assert any(row.kind == "guardian" and row.decision == "block" for row in audit)
        assert any((row.detail or {}).get("status") == "error" for row in audit)
        assert not repos.approvals
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_async_audit_and_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = uuid4().hex
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path), smtp_credential=value)
    runtime = build_graph_runtime(settings)
    repos = MemoryRepositories()
    runtime.audit_repositories = repos
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.tools["send_email"] = Decision.allow
    monkeypatch.setattr("dot.assembly.load_pack", lambda _: loaded)

    class Email:
        def send(self, *, to, subject, body, credential):
            assert credential == value
            return value

    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), email=Email())
    model = ScriptedChatModel(
        script=[tools(call("send_email", to="x@example.com", subject="hi", body="hello")), say("done")]
    )
    try:
        dot = create_dot(repos, runtime, "research-analyst", "owner")
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model, deps=deps)
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Send an email"}]}, {"configurable": {"thread_id": dot.thread_id}}
        )
        trail = repos.list_audit(dot.dot_id)
        assert [event.kind for event in trail] == [
            "tool_call",
            "policy",
            "guardian",
            "tool_call",
            "policy",
            "tool_call",
        ]
        assert trail[-1].detail["phase"] == "result"
        assert value not in json.dumps(
            [message.model_dump() for request in [*model.seen, *model.structured_seen] for message in request]
        )
    finally:
        runtime.close()


def test_audit_write_failure_prevents_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    runtime = build_graph_runtime(settings)
    repos = MemoryRepositories()
    runtime.audit_repositories = repos
    reached = []
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.tools["send_email"] = Decision.allow
    monkeypatch.setattr("dot.assembly.load_pack", lambda _: loaded)

    class Email:
        def send(self, **kwargs):
            reached.append("sent")
            return "sent"

    class Broker:
        def resolve(self, handle):
            return uuid4().hex

    def unavailable(event):
        raise RuntimeError("audit storage unavailable")

    try:
        dot = create_dot(repos, runtime, "research-analyst", "owner")
        model = ScriptedChatModel(
            script=[tools(call("send_email", to="x@example.com", subject="hi", body="hello")), say("done")]
        )
        deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), credentials=Broker(), email=Email())
        monkeypatch.setattr(repos, "append_audit", unavailable)
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model, deps=deps)
        with pytest.raises(RuntimeError, match="audit storage unavailable"):
            agent.invoke(
                {"messages": [{"role": "user", "content": "Send"}]}, {"configurable": {"thread_id": dot.thread_id}}
            )
        assert not reached
    finally:
        runtime.close()
