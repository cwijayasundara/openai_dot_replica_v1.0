"""End a replay at the proposal being judged, before any tool with an effect runs.

Replay re-makes a past proposal against scratch state. This runs after every
model call, ahead of the Guardian and approval review, so a replay reaches no
review, no approval card and no consequential tool. Only plain reads may run,
so a skill or wiki edit that works once it is read gets the chance to work.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime


class ReplayStop(AgentMiddleware[AgentState[Any], Any, Any]):
    def __init__(self, targets: frozenset[str], max_model_calls: int) -> None:
        super().__init__()
        # The tools the judged episode is about.
        self.targets = targets
        self.max_model_calls = max_model_calls
        # Assembly binds this to the compiled agent's policy. Until then nothing may run.
        self.may_run: Callable[[str], bool] = lambda _name: False
        self.calls = 0
        self.stop: str | None = None
        self.message: AIMessage | None = None

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        del runtime
        self.calls += 1
        last = state["messages"][-1]
        calls = last.tool_calls if isinstance(last, AIMessage) else []
        stopping = next((c["name"] for c in calls if c["name"] in self.targets or not self.may_run(c["name"])), None)
        if not calls:
            self.stop = "reply"
        elif stopping is not None:
            self.stop = f"tool:{stopping}"
        elif self.calls >= self.max_model_calls:
            self.stop = "budget"
        else:
            return None
        self.message = last if isinstance(last, AIMessage) else None
        return {"jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
