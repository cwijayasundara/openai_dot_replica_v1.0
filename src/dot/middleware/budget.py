"""A scheduled run's model-call and token ceiling. See design section 7.

One ``RunBudget`` is shared by the supervisor and every subagent of a run, so
calls made inside ``task`` count. When the next call would exceed it, the agent
ends without a message: nothing the budget says is mistaken for the dot's reply.
"""

from __future__ import annotations

import threading
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from dot.persistence.db import Json


class RunBudget:
    def __init__(self, max_model_calls: int, max_tokens: int) -> None:
        self.max_model_calls = max_model_calls
        self.max_tokens = max_tokens
        self._lock = threading.Lock()
        self._calls = 0
        self._tokens = 0
        self._stopped = False

    def admit(self) -> bool:
        """Whether one more model call is allowed. A refusal stops the run for good."""
        with self._lock:
            if self._calls >= self.max_model_calls or self._tokens >= self.max_tokens:
                self._stopped = True
            return not self._stopped

    def charge(self, message: object) -> None:
        tokens = 0
        if isinstance(message, AIMessage) and message.usage_metadata:
            tokens = int(message.usage_metadata.get("total_tokens", 0))
        with self._lock:
            self._calls += 1
            self._tokens += tokens

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def usage(self) -> Json:
        with self._lock:
            return {
                "model_calls": self._calls,
                "tokens": self._tokens,
                "max_model_calls": self.max_model_calls,
                "max_tokens": self.max_tokens,
            }


class BudgetMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    def __init__(self, budget: RunBudget) -> None:
        super().__init__()
        self.budget = budget

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        del state, runtime
        return None if self.budget.admit() else {"jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    def after_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        del runtime
        messages = state.get("messages") or []
        self.budget.charge(messages[-1] if messages else None)
        return None

    async def aafter_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
