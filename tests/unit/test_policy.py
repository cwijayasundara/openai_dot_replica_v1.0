"""Rule precedence, fail-closed effects, and real sync/async tool interception."""

import pytest
from langchain.agents import create_agent
from langchain_core.tools import tool

from dot.middleware.policy import PolicyMiddleware
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Decision, Policy
from dot.safety.policy import FILESYSTEM_EFFECTS, PolicyResolver, resolve_decision
from dot.tools.effects import Effect
from tests.support.scripted_model import ScriptedChatModel, call, say, tools


@pytest.mark.parametrize(
    ("name", "effect", "expected"),
    [
        ("web_search", Effect.read, Decision.allow),
        ("draft_email", Effect.draft, Decision.allow),
        ("write_file", Effect.write, Decision.approve),
        ("slack_post", Effect.external, Decision.approve),
        ("purchase", Effect.financial, Decision.approve),
        ("resolve_secret", Effect.credential, Decision.block),
        ("send_email", Effect.read, Decision.approve),
        ("delete_report", Effect.read, Decision.block),
        ("DELETE_report", Effect.read, Decision.allow),
        ("unknown", None, Decision.block),
        ("send_email", None, Decision.block),
    ],
)
def test_rule_and_effect_table(name: str, effect: Effect | None, expected: Decision) -> None:
    policy = load_pack(REPO_ROOT / "packs" / "research-analyst").policy
    resolver = PolicyResolver(policy, {name: effect} if effect is not None else {})
    assert resolver.decision(name) is expected
    assert resolve_decision(policy, name, effect) is expected


def test_first_declared_match_and_immutable_snapshot() -> None:
    policy = load_pack(REPO_ROOT / "packs" / "research-analyst").policy.model_copy(
        update={"tools": {"send_*": Decision.block, "send_email": Decision.allow}}
    )
    effects = {"send_email": Effect.external}
    resolver = PolicyResolver(policy, effects)
    assert resolver.decision("send_email") is Decision.block
    policy.tools.clear()
    policy.defaults.external = Decision.allow
    effects.clear()
    assert resolver.decision("send_email") is Decision.block


def test_approval_map_is_concrete_and_scoped() -> None:
    policy = load_pack(REPO_ROOT / "packs" / "research-analyst").policy
    resolver = PolicyResolver(policy, FILESYSTEM_EFFECTS | {"send_email": Effect.external, "task": Effect.read})
    approval = resolver.approval_map(["execute", "write_file", "send_email", "read_file", "unknown", "task"])
    assert set(approval) == {"execute", "write_file", "send_email"}
    assert all(config == {"allowed_decisions": ["approve", "edit", "reject"]} for config in approval.values())
    assert "delete_*" not in approval


def blocked_agent(policy: Policy):
    reached: list[str] = []

    @tool
    def delete_report() -> str:
        """Delete a report."""
        reached.append("delete_report")
        return "deleted"

    @tool
    def untagged() -> str:
        """An untagged tool must not execute."""
        reached.append("untagged")
        return "bad"

    model = ScriptedChatModel(script=[tools(call("delete_report"), call("untagged")), say("done")])
    resolver = PolicyResolver(policy, {"delete_report": Effect.write})
    return create_agent(model, tools=[delete_report, untagged], middleware=[PolicyMiddleware(resolver)]), reached


def test_blocked_calls_never_reach_tool() -> None:
    policy = load_pack(REPO_ROOT / "packs" / "research-analyst").policy
    agent, reached = blocked_agent(policy)
    result = agent.invoke({"messages": [{"role": "user", "content": "delete"}]})
    assert not reached
    results = [message for message in result["messages"] if message.type == "tool"]
    assert len(results) == 2
    assert all(message.status == "error" and "blocked by policy" in message.content for message in results)


@pytest.mark.asyncio
async def test_async_blocked_calls_never_reach_tool() -> None:
    policy = load_pack(REPO_ROOT / "packs" / "research-analyst").policy
    agent, reached = blocked_agent(policy)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "delete"}]})
    assert not reached
    assert all("blocked by policy" in message.content for message in result["messages"] if message.type == "tool")
