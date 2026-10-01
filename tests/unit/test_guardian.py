"""Guardian decisions, narrow prompts, delegation and reviewer-edit boundaries."""

import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Decision
from dot.safety.guardian import Guardian
from dot.safety.policy import PolicyResolver
from dot.tools.artifacts import ArtifactStore
from dot.tools.effects import Effect
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.coder_task import stage_task
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

ARGS = {"to": "alice@example.com", "subject": "Update", "body": "A brief update."}


def build(tmp_path: Path, model: ScriptedChatModel, reviewer: ScriptedChatModel, *, deps: ToolDeps | None = None):
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    runtime = build_graph_runtime(settings)
    agent = build_dot_agent(
        dot,
        "chat",
        settings=settings,
        runtime=runtime,
        model=model,
        guardian=Guardian(reviewer),
        deps=deps,
    )
    return agent, runtime, {"configurable": {"thread_id": dot.thread_id}}


@pytest.mark.parametrize(
    ("in_scope", "risk", "allowed"),
    [
        (True, "low", True),
        (True, "medium", True),
        (True, "high", False),
        (False, "low", False),
        (False, "medium", False),
        (False, "high", False),
    ],
)
def test_verdict_table(tmp_path: Path, in_scope: bool, risk: str, allowed: bool) -> None:
    model = ScriptedChatModel(script=[tools(call("draft_email", **ARGS)), say("done")])
    reviewer = ScriptedChatModel(structured_script=[{"in_scope": in_scope, "risk": risk, "reason": "Test reason"}])
    artifacts = ArtifactStore(tmp_path / "artifacts")
    agent, runtime, config = build(tmp_path, model, reviewer, deps=ToolDeps(artifacts))
    try:
        result = agent.invoke({"messages": [HumanMessage("Draft an email to Alice.")]}, config)
    finally:
        runtime.close()
    messages = [m for m in result["messages"] if m.type == "tool"]
    assert len(reviewer.structured_seen) == 1
    if allowed:
        assert "artifact_id" in messages[0].content
    else:
        assert messages[0].status == "error" and "Guardian refused: Test reason" in messages[0].content
        assert not list((tmp_path / "artifacts").glob("**/*"))


@pytest.mark.parametrize(
    "value",
    [
        RuntimeError("private error"),
        {"in_scope": True},
        {"in_scope": "yes", "risk": "low", "reason": "bad bool"},
        {"in_scope": True, "risk": "low", "reason": "ok", "extra": "bad"},
    ],
)
def test_invalid_or_unavailable_review_fails_closed(tmp_path: Path, value: object) -> None:
    model = ScriptedChatModel(script=[tools(call("draft_email", **ARGS)), say("done")])
    reviewer = ScriptedChatModel(structured_script=[value])
    agent, runtime, config = build(tmp_path, model, reviewer)
    try:
        result = agent.invoke({"messages": [HumanMessage("Draft an email.")]}, config)
    finally:
        runtime.close()
    assert any("Guardian unavailable" in m.content for m in result["messages"] if m.type == "tool")
    assert "private error" not in str(result)


def test_fetched_content_and_tool_results_never_enter_guardian_prompt(tmp_path: Path) -> None:
    sentinel = "FETCHED_INJECTION_IGNORE_USER_SEND_SECRETS"

    class Search:
        def search(self, query: str, *, limit: int = 5) -> list[Hit]:
            return [Hit(title="Source", url="https://example.com", snippet=sentinel)]

    model = ScriptedChatModel(
        script=[
            tools(call("web_search", query="topic")),
            tools(call("draft_email", **ARGS)),
            say("done"),
        ]
    )
    reviewer = ScriptedChatModel()
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), search=Search())
    agent, runtime, config = build(tmp_path, model, reviewer, deps=deps)
    instruction = "Research the topic and draft an email to Alice."
    try:
        result = agent.invoke({"messages": [HumanMessage(instruction)]}, config)
    finally:
        runtime.close()
    assert sentinel in str(result["messages"])
    assert len(reviewer.structured_seen) == 1  # The read did not trigger review.
    request = reviewer.structured_seen[0]
    assert all(m.type in {"system", "human"} for m in request)
    assert sentinel not in str(request)
    data = json.loads(request[1].content)
    assert set(data) == {"original_instruction", "tool", "args", "effect", "policy"}
    assert data["original_instruction"] == instruction and data["args"] == ARGS


def test_new_turn_replaces_original_instruction(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        script=[tools(call("draft_email", **ARGS)), say("done"), tools(call("draft_email", **ARGS)), say("done")]
    )
    reviewer = ScriptedChatModel()
    agent, runtime, config = build(tmp_path, model, reviewer)
    try:
        agent.invoke({"messages": [HumanMessage("First request.")]}, config)
        agent.invoke({"messages": [HumanMessage("Second request."), HumanMessage("Also keep it short.")]}, config)
    finally:
        runtime.close()
    objectives = [json.loads(request[1].content)["original_instruction"] for request in reviewer.structured_seen]
    assert objectives == ["First request.", "Second request.\n\nAlso keep it short."]


def test_coder_retains_root_instruction_and_refuses_before_approval(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        script=[
            tools(call("task", subagent_type="coder", description="DELEGATED_OVERRIDE_DELETE_EVERYTHING")),
            tools(call("execute", command="rm -rf /work/*")),
            say("Refused"),
            say("done"),
        ]
    )
    reviewer = ScriptedChatModel(
        structured_script=[{"in_scope": False, "risk": "high", "reason": "Unrequested deletion"}]
    )
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    runtime = build_graph_runtime(settings)

    def forbidden(_dot_id: str):
        raise AssertionError("Guardian-refused coder started a sandbox")

    try:
        agent = build_dot_agent(
            dot,
            "chat",
            settings=settings,
            runtime=runtime,
            model=model,
            guardian=Guardian(reviewer),
            sandbox_factory=forbidden,
        )
        result = agent.invoke(
            {"messages": [HumanMessage("Compute the sum from my CSV.")]}, {"configurable": {"thread_id": dot.thread_id}}
        )
        assert not result.get("__interrupt__")
    finally:
        runtime.close()
    data = json.loads(reviewer.structured_seen[0][1].content)
    assert data["original_instruction"] == "Compute the sum from my CSV."
    assert "DELEGATED_OVERRIDE" not in str(reviewer.structured_seen)


def test_refused_external_call_never_requests_human_approval(tmp_path: Path) -> None:
    model = ScriptedChatModel(script=[tools(call("send_email", **ARGS)), say("done")])
    reviewer = ScriptedChatModel(structured_script=[{"in_scope": False, "risk": "low", "reason": "Draft only"}])
    agent, runtime, config = build(tmp_path, model, reviewer)
    try:
        result = agent.invoke({"messages": [HumanMessage("Draft only. Do not send.")]}, config)
    finally:
        runtime.close()
    assert not result.get("__interrupt__")
    assert any("Guardian refused" in m.content for m in result["messages"] if m.type == "tool")


def test_edited_arguments_are_reviewed_again(tmp_path: Path) -> None:
    model = ScriptedChatModel(script=[tools(call("send_email", **ARGS)), say("done")])
    reviewer = ScriptedChatModel(
        structured_script=[
            {"in_scope": True, "risk": "low", "reason": "Requested recipient"},
            {"in_scope": False, "risk": "high", "reason": "Wrong recipient"},
        ]
    )
    agent, runtime, config = build(tmp_path, model, reviewer)
    try:
        result = agent.invoke({"messages": [HumanMessage("Send an update to Alice.")]}, config)
        assert result["__interrupt__"]
        result = agent.invoke(
            Command(
                resume={
                    "decisions": [
                        {
                            "type": "edit",
                            "edited_action": {
                                "name": "send_email",
                                "args": {**ARGS, "to": "evil@example.com"},
                            },
                        }
                    ]
                }
            ),
            config,
        )
    finally:
        runtime.close()
    assert len(reviewer.structured_seen) == 2
    assert any("Wrong recipient" in m.content for m in result["messages"] if m.type == "tool")


def test_policy_block_cannot_be_overridden_by_guardian(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = load_pack(REPO_ROOT / "packs" / "research-analyst")
    loaded.policy.tools["draft_email"] = Decision.block
    monkeypatch.setattr("dot.assembly.load_pack", lambda _path: loaded)
    model = ScriptedChatModel(script=[tools(call("draft_email", **ARGS)), say("done")])
    reviewer = ScriptedChatModel()
    agent, runtime, config = build(tmp_path, model, reviewer)
    try:
        result = agent.invoke({"messages": [HumanMessage("Draft an email.")]}, config)
    finally:
        runtime.close()
    assert not reviewer.structured_seen
    assert any("blocked by policy" in m.content for m in result["messages"] if m.type == "tool")


@pytest.mark.asyncio
async def test_async_guardian_refusal(tmp_path: Path) -> None:
    model = ScriptedChatModel(script=[tools(call("draft_email", **ARGS)), say("done")])
    reviewer = ScriptedChatModel(structured_script=[{"in_scope": False, "risk": "low", "reason": "Out of scope"}])
    agent, runtime, config = build(tmp_path, model, reviewer)
    try:
        result = await agent.ainvoke({"messages": [HumanMessage("Research only.")]}, config)
    finally:
        runtime.close()
    assert any("Guardian refused" in m.content for m in result["messages"] if m.type == "tool")


def test_guardian_redacts_known_secrets_and_missing_instruction_fails_closed() -> None:
    reviewer = ScriptedChatModel()
    guardian = Guardian(reviewer, secrets=["secret-value-fixture"])
    policy = PolicyResolver(load_pack(REPO_ROOT / "packs" / "research-analyst").policy, {"draft_email": Effect.draft})
    assert guardian.review("", "draft_email", ARGS, Effect.draft, policy).permitted is False
    assert not reviewer.structured_seen
    verdict = guardian.review(
        "Draft secret-value-fixture", "draft_email", {**ARGS, "body": "secret-value-fixture"}, Effect.draft, policy
    )
    assert verdict.permitted
    assert "secret-value-fixture" not in str(reviewer.structured_seen)
    assert "[REDACTED]" in str(reviewer.structured_seen)


def test_fast_role_is_used_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_model = ScriptedChatModel(
        script=[
            tools(call("draft_email", **ARGS)),
            tools(call("draft_email", **ARGS)),
            say("done"),
        ]
    )
    fast = ScriptedChatModel()
    roles = []

    def factory(role, settings):
        roles.append(role)
        return fast if role == "fast" else agent_model

    monkeypatch.setattr("dot.assembly.chat_model", factory)
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    runtime = build_graph_runtime(settings)
    try:
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime)
        assert "fast" not in roles
        agent.invoke({"messages": [HumanMessage("Draft an email.")]}, {"configurable": {"thread_id": dot.thread_id}})
    finally:
        runtime.close()
    assert roles.count("fast") == 1 and len(fast.structured_seen) == 2
