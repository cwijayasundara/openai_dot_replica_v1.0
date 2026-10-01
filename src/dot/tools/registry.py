"""Tools registered with an effect. ``for_profile`` is what a turn is allowed to call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from langchain_core.tools import BaseTool

from .effects import Effect

# ``execute`` is the sandbox shell named by the coder subagent. It changes /work.
NATIVE_EFFECTS: dict[str, Effect] = {
    "web_search": Effect.read,
    "fetch_url": Effect.read,
    "write_report": Effect.draft,
    "draft_email": Effect.draft,
    "send_email": Effect.external,
    "slack_post": Effect.external,
    "execute": Effect.write,
}


class CapabilityProfile(Protocol):
    tools: Literal["*"] | list[str] | None
    effects: list[Effect] | None


@dataclass
class _Entry:
    effect: Effect | None
    tool: BaseTool | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(self, name: str, effect: Effect | None, tool: BaseTool | None = None) -> None:
        if tool is not None and tool.name != name:
            raise ValueError(f"tool name {tool.name!r} does not match {name!r}")
        self._entries[name] = _Entry(effect, tool)

    def copy(self) -> ToolRegistry:
        other = ToolRegistry()
        other._entries = {name: _Entry(entry.effect, entry.tool) for name, entry in self._entries.items()}
        return other

    def effects(self) -> dict[str, Effect]:
        """A copy of every tagged tool's effect, including backend-supplied tools."""
        return {name: entry.effect for name, entry in self._entries.items() if entry.effect is not None}

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def effect(self, name: str) -> Effect | None:
        """Effect for a known tool. None means the tool is untagged.

        Raises KeyError when the name was never registered.
        """
        return self._entries[name].effect

    def for_profile(self, profile: CapabilityProfile) -> list[BaseTool]:
        """Tools this profile may see. Untagged tools and tools with no callable are omitted.

        A list of names restricts the set. ``*`` or an absent list starts from every
        callable tool. An effect list then keeps only those effects.
        """
        names = {name for name, entry in self._entries.items() if entry.tool is not None and entry.effect is not None}
        if isinstance(profile.tools, list):
            names &= set(profile.tools)
        if profile.effects is not None:
            allowed = set(profile.effects)
            names = {name for name in names if self._entries[name].effect in allowed}
        offered: list[BaseTool] = []
        for name in sorted(names):
            tool = self._entries[name].tool
            if tool is not None:
                offered.append(tool)
        return offered


def builtin_registry() -> ToolRegistry:
    """Effects only. Callables are attached by ``native_registry``."""
    registry = ToolRegistry()
    for name, effect in NATIVE_EFFECTS.items():
        registry.register(name, effect)
    return registry
