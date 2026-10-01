"""Cancellation and ``update_job`` messages for a background job's agent.

The job row is read before every model call and every tool call. A cancelled
job ends at the next model boundary and runs no further tools. New updates
are appended to the conversation before the next model call; the count
already applied lives in graph state, so a resumed job does not repeat them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from dot.persistence.db import Job


class JobControlState(AgentState[Any]):
    job_updates_applied: NotRequired[int]


class JobControlMiddleware(AgentMiddleware[JobControlState, Any, Any]):
    state_schema = JobControlState

    def __init__(self, current: Callable[[], Job]) -> None:
        super().__init__()
        self._current = current

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: JobControlState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        del runtime
        job = self._current()
        if job.status == "cancelled":
            return {"jump_to": "end"}
        applied = state.get("job_updates_applied", 0)
        pending = job.updates[applied:]
        if not pending:
            return None
        return {
            "messages": [HumanMessage(content=f"[update from the dot] {update['message']}") for update in pending],
            "job_updates_applied": len(job.updates),
        }

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: JobControlState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    def _cancelled(self, request: ToolCallRequest) -> ToolMessage | None:
        if self._current().status != "cancelled":
            return None
        return ToolMessage(
            content="The job was cancelled; this tool did not run.",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call.get("id") or "",
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        stopped = self._cancelled(request)
        return stopped if stopped is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        stopped = self._cancelled(request)
        return stopped if stopped is not None else await handler(request)
