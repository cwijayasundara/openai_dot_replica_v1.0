"""Scripted-model acceptance for the assembled supervisor. No live model and no sandbox."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from deepagents.backends.utils import create_file_data
from langchain_core.messages import HumanMessage, ToolMessage

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack, memories_namespace, seed_store
from dot.persistence.db import Dot, MemoryRepositories
from dot.sandbox.base import RunSandbox
from dot.surfaces.dots import create_dot
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

_HIDDEN = {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "delete", "execute"}


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        self.queries.append(query)
        return [Hit(title="Open Dot", url="https://example.com/dot", snippet="A runtime for research.")]


class _Email:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, *, to: str, subject: str, body: str, credential: str) -> str:
        self.sent.append(to)
        return "msg-1"


def _dot() -> Dot:
    return Dot(
        dot_id="dot-1",
        owner_user_id="user-1",
        pack_name="research-analyst",
        pack_version="0",
        thread_id="thread-1",
        status="active",
        created_at=datetime.now(UTC),
    )


def _factory(started: list[str]):
    def factory(dot_id: str) -> RunSandbox:
        started.append(dot_id)
        raise RuntimeError(f"sandbox for {dot_id} started")

    return factory


def _build(tmp_path: Path, profile: str, model: ScriptedChatModel, deps: ToolDeps, started: list[str]):
    settings = Settings(_env_file=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    runtime = build_graph_runtime(settings)
    dot = _dot()
    seed_store(load_pack(REPO_ROOT / "packs" / dot.pack_name), runtime.store, dot.dot_id)
    agent = build_dot_agent(
        dot,
        profile,
        settings=settings,
        runtime=runtime,
        deps=deps,
        model=model,
        sandbox_factory=_factory(started),
    )
    return agent, dot, runtime


def _offered(model: ScriptedChatModel) -> set[str]:
    assert model.offered, "the model was never called"
    return set(model.offered[0])


def _task_description(model: ScriptedChatModel) -> str:
    for bound in model.requests:
        for tool in bound:
            if getattr(tool, "name", None) == "task":
                return str(getattr(tool, "description", ""))
    return ""


def test_chat_turn_calls_a_tool_and_answers(tmp_path: Path) -> None:
    search = _Search()
    started: list[str] = []
    model = ScriptedChatModel(
        script=[
            tools(call("web_search", query="open dot")),
            say("Here is the answer."),
        ]
    )
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), search=search)
    agent, dot, runtime = _build(tmp_path, "chat", model, deps, started)
    try:
        result = agent.invoke(
            {"messages": [HumanMessage("Find something about open dot")]},
            config={"configurable": {"thread_id": dot.thread_id}},
        )
    finally:
        runtime.close()

    offered = _offered(model)
    assert "web_search" in offered
    assert "send_email" in offered
    assert offered.isdisjoint(_HIDDEN)
    assert search.queries == ["open dot"]
    assert started == []
    messages = result["messages"]
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert any(message.name == "web_search" and "untrusted-data" in str(message.content) for message in tool_messages)
    assert messages[-1].content == "Here is the answer."
    assert "research-method" in str(model.seen[0][0].content)
    description = _task_description(model)
    assert "researcher" in description
    assert "coder" in description
    assert "- general-purpose:" not in description


def test_tool_outside_the_profile_is_not_offered_or_callable(tmp_path: Path) -> None:
    email = _Email()
    started: list[str] = []
    model = ScriptedChatModel(
        script=[
            tools(
                call("send_email", to="a@example.com", subject="hi", body="hello"),
                call("execute", command="echo hi"),
            ),
            say("done"),
        ]
    )
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), email=email)
    agent, dot, runtime = _build(tmp_path, "sweep", model, deps, started)
    try:
        result = agent.invoke(
            {"messages": [HumanMessage("Run the sweep")]},
            config={"configurable": {"thread_id": dot.thread_id}},
        )
    finally:
        runtime.close()

    offered = _offered(model)
    assert {"web_search", "fetch_url", "task"} <= offered
    assert "send_email" not in offered
    assert "draft_email" not in offered
    assert "slack_post" not in offered
    assert offered.isdisjoint(_HIDDEN)
    assert email.sent == []
    assert started == []
    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    denied = {message.name: str(message.content) for message in tool_messages}
    assert "not allowed" in denied["send_email"]
    assert "not allowed" in denied["execute"]
    assert result["messages"][-1].content == "done"
    description = _task_description(model)
    assert "researcher" in description
    assert "coder" not in description
    assert "- general-purpose:" not in description


def test_memory_and_skill_edits_reach_the_next_turn_on_the_same_thread(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))  # type: ignore[call-arg]
    repos = MemoryRepositories()
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    dot = create_dot(repos, runtime, "research-analyst", "owner")
    model = ScriptedChatModel(script=[say("one"), say("two")])
    config = {"configurable": {"thread_id": dot.thread_id}}
    namespace = memories_namespace(dot.dot_id)
    skill = "---\nname: tone\ndescription: {}\n---\nBody.\n"

    runtime.store.put(namespace, "/AGENTS.md", dict(create_file_data("Prefer PREF-ONE.")))
    runtime.store.put(namespace, "/skills/tone/SKILL.md", dict(create_file_data(skill.format("SKILL-ONE"))))
    build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model).invoke(
        {"messages": [HumanMessage("a")]}, config
    )
    runtime.store.put(namespace, "/AGENTS.md", dict(create_file_data("Prefer PREF-TWO.")))
    runtime.store.put(namespace, "/skills/tone/SKILL.md", dict(create_file_data(skill.format("SKILL-TWO"))))
    build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model).invoke(
        {"messages": [HumanMessage("b")]}, config
    )

    first, second = (str(seen[0].content) for seen in model.seen)
    assert "PREF-ONE" in first and "SKILL-ONE" in first
    assert "PREF-TWO" in second and "PREF-ONE" not in second
    assert "SKILL-TWO" in second and "SKILL-ONE" not in second
