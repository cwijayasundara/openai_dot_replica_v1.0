"""Reload memory and the skills list at the start of every turn.

deepagents loads ``AGENTS.md`` and the skills list once and keeps them in
thread state. A dot has one continuous thread, so without this a memory edit
(accepted by reflection, rolled back, or made by hand) would never reach it.
Each class keeps the built-in's name, so it replaces that middleware in place.
"""

from __future__ import annotations

from typing import Any

from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime


class TurnMemoryMiddleware(MemoryMiddleware):
    @property
    def name(self) -> str:
        return MemoryMiddleware.__name__

    def before_agent(self, state: Any, runtime: Runtime, config: RunnableConfig) -> Any:  # type: ignore[override]
        return super().before_agent(_without(state, "memory_contents"), runtime, config)

    async def abefore_agent(self, state: Any, runtime: Runtime, config: RunnableConfig) -> Any:  # type: ignore[override]
        return await super().abefore_agent(_without(state, "memory_contents"), runtime, config)


class TurnSkillsMiddleware(SkillsMiddleware):
    @property
    def name(self) -> str:
        return SkillsMiddleware.__name__

    def before_agent(self, state: Any, runtime: Runtime, config: RunnableConfig) -> Any:  # type: ignore[override]
        return super().before_agent(_without(state, "skills_metadata"), runtime, config)

    async def abefore_agent(self, state: Any, runtime: Runtime, config: RunnableConfig) -> Any:  # type: ignore[override]
        return await super().abefore_agent(_without(state, "skills_metadata"), runtime, config)


def _without(state: Any, key: str) -> Any:
    return {name: value for name, value in dict(state).items() if name != key}
