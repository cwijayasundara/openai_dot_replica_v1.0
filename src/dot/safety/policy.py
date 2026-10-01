"""Resolve pack rules identically at load time and execution time."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from langchain.agents.middleware import InterruptOnConfig

from dot.packs.schema import Decision, Policy
from dot.tools.effects import Effect

# Deep Agents supplies these callables itself; they still need explicit effects.
FILESYSTEM_EFFECTS = {
    "ls": Effect.read,
    "read_file": Effect.read,
    "glob": Effect.read,
    "grep": Effect.read,
    "write_file": Effect.write,
    "edit_file": Effect.write,
    "delete": Effect.write,
    "execute": Effect.write,
}


def _matching_rule(rules: Iterable[tuple[str, Decision]], tool_name: str, fallback: Decision) -> Decision:
    for pattern, decision in rules:
        if fnmatch.fnmatchcase(tool_name, pattern):
            return decision
    return fallback


def resolve_decision(policy: Policy, tool_name: str, effect: Effect | None) -> Decision:
    """Untagged tools are blocked; otherwise the first matching rule wins."""
    if effect is None:
        return Decision.block
    return _matching_rule(policy.tools.items(), tool_name, policy.defaults.for_effect(effect))


@dataclass(frozen=True, slots=True, init=False)
class PolicyResolver:
    """Immutable policy/effect snapshot for a compiled agent."""

    rules: tuple[tuple[str, Decision], ...]
    defaults: tuple[tuple[Effect, Decision], ...]
    effects: tuple[tuple[str, Effect], ...]

    def __init__(self, policy: Policy, effects: Mapping[str, Effect]) -> None:
        object.__setattr__(self, "rules", tuple(policy.tools.items()))
        object.__setattr__(self, "defaults", tuple((effect, policy.defaults.for_effect(effect)) for effect in Effect))
        object.__setattr__(self, "effects", tuple(effects.items()))

    def decision(self, tool_name: str) -> Decision:
        effect = dict(self.effects).get(tool_name)
        if effect is None:
            return Decision.block
        return _matching_rule(self.rules, tool_name, dict(self.defaults)[effect])

    def approval_map(self, names: Iterable[str]) -> dict[str, bool | InterruptOnConfig]:
        """Only concrete, offered tools may create approval interrupts."""
        return {
            name: {"allowed_decisions": ["approve", "edit", "reject"]}
            for name in names
            if self.decision(name) is Decision.approve
        }
