"""A deterministic block backstop before any tool code executes."""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from dot.packs.schema import Decision
from dot.safety.audit import AuditWriter
from dot.safety.policy import PolicyResolver


class PolicyMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, policy: PolicyResolver, audit: AuditWriter | None = None) -> None:
        super().__init__()
        self.policy = policy
        self.audit = audit

    def _denial(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call["name"]
        if self.audit is not None:
            self.audit.record(
                "policy",
                name,
                state=request.state,
                decision=self.policy.decision(name).value,
                detail={
                    "phase": "execution",
                    "tool_call_id": request.tool_call.get("id"),
                    "args": request.tool_call["args"],
                },
            )
        if self.policy.decision(name) is not Decision.block:
            return None
        return ToolMessage(
            content=f"Tool {name!r} is blocked by policy.",
            name=name,
            tool_call_id=request.tool_call.get("id") or "",
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        denied = self._denial(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        denied = self._denial(request)
        return denied if denied is not None else await handler(request)
