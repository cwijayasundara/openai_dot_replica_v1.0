"""Policy and HITL in the one assembly, with a fake external transport."""

from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Decision
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps
from tests.support.coder_task import SCRIPT, SCRIPT_PATH, stage_task
from tests.support.scripted_model import ScriptedChatModel, call, say, tools
from tests.unit.test_coder_subagent import RecordingSandbox


class Email:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, *, to: str, subject: str, body: str, credential: str) -> str:
        self.sent.append(to)
        return "message-1"


class Credentials:
    def resolve(self, handle: str) -> str:
        return "fixture-only"


@pytest.mark.parametrize("decision", ["approve", "reject", "edit"])
def test_external_call_waits_for_review(tmp_path: Path, decision: str) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    email = Email()
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), email=email, credentials=Credentials())
    model = ScriptedChatModel(
        script=[
            tools(call("send_email", to="original@example.com", subject="hi", body="hello")),
            say("done"),
        ]
    )
    runtime = build_graph_runtime(settings)
    config = {"configurable": {"thread_id": dot.thread_id}}
    try:
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model, deps=deps)
        result = agent.invoke({"messages": [{"role": "user", "content": "send"}]}, config)
        assert not email.sent and model.calls == 1
        pending = result["__interrupt__"][0].value
        assert pending["action_requests"][0]["name"] == "send_email"
        assert pending["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]
        response: dict[str, Any] = {"type": decision}
        if decision == "edit":
            response["edited_action"] = {
                "name": "send_email",
                "args": {"to": "edited@example.com", "subject": "hi", "body": "hello"},
            }
        agent.invoke(Command(resume={"decisions": [response]}), config)
        expected = (
            [] if decision == "reject" else ["edited@example.com" if decision == "edit" else "original@example.com"]
        )
        assert email.sent == expected
    finally:
        runtime.close()


@pytest.mark.parametrize("redirect", ["slack_post", "execute"])
def test_reviewer_redirect_is_rechecked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redirect: str,
) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    loaded = load_pack(REPO_ROOT / "packs" / dot.pack_name)
    loaded.policy.tools["slack_post"] = Decision.block
    monkeypatch.setattr("dot.assembly.load_pack", lambda _path: loaded)
    reached: list[str] = []

    class Slack:
        def post(self, *, channel: str, text: str) -> str:
            reached.append(channel)
            return "sent"

    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), slack=Slack())
    model = ScriptedChatModel(
        script=[
            tools(call("send_email", to="x@example.com", subject="hi", body="hello")),
            say("done"),
        ]
    )
    runtime = build_graph_runtime(settings)
    config = {"configurable": {"thread_id": dot.thread_id}}
    try:
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model, deps=deps)
        result = agent.invoke({"messages": [{"role": "user", "content": "send"}]}, config)
        assert result["__interrupt__"]
        result = agent.invoke(
            Command(
                resume={
                    "decisions": [
                        {
                            "type": "edit",
                            "edited_action": {
                                "name": redirect,
                                "args": {"channel": "C1", "text": "no"}
                                if redirect == "slack_post"
                                else {"command": "echo no"},
                            },
                        }
                    ]
                }
            ),
            config,
        )
        assert not reached
        reason = "blocked by policy" if redirect == "slack_post" else "not allowed"
        assert any(reason in str(message.content) for message in result["messages"] if message.type == "tool")
    finally:
        runtime.close()


def test_coder_write_pauses_before_starting_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    supervisor = ScriptedChatModel(
        script=[
            tools(call("task", subagent_type="coder", description="Write a script")),
            say("done"),
        ]
    )
    heavy = ScriptedChatModel(script=[tools(call("write_file", file_path=SCRIPT_PATH, content=SCRIPT)), say("written")])
    monkeypatch.setattr("dot.assembly.chat_model", lambda role, _settings: heavy if role == "heavy" else supervisor)
    sandbox = RecordingSandbox()
    started: list[str] = []

    def factory(dot_id: str) -> RecordingSandbox:
        started.append(dot_id)
        return sandbox

    runtime = build_graph_runtime(settings)
    config = {"configurable": {"thread_id": dot.thread_id}}
    try:
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, sandbox_factory=factory)
        result = agent.invoke({"messages": [{"role": "user", "content": "code"}]}, config)
        assert result["__interrupt__"][0].value["action_requests"][0]["name"] == "write_file"
        assert not started and not sandbox.files
        agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert sandbox.files[SCRIPT_PATH] == SCRIPT
    finally:
        runtime.close()
