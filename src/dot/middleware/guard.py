"""Tool surface policy: what a model is offered, and what it may call.

Filtering the request keeps a hidden tool out of the model's view. The call
guard is the backstop when a model names a tool it was never shown.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from dot.safety.audit import AuditWriter

FS_TOOLS = frozenset({"ls", "read_file", "write_file", "edit_file", "glob", "grep", "delete", "execute"})


@dataclass(frozen=True, slots=True)
class ToolSurfacePolicy:
    allowed: frozenset[str] | None = None
    hidden: frozenset[str] = frozenset()
    # Subagents the ``task`` tool may start. None means any.
    subagents: frozenset[str] | None = None

    def permits(self, name: str) -> bool:
        return name not in self.hidden and (self.allowed is None or name in self.allowed)

    def permits_call(self, name: str, args: dict[str, Any]) -> bool:
        if not self.permits(name):
            return False
        if name == "task" and self.subagents is not None:
            return args.get("subagent_type") in self.subagents
        return True


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        function_name = function.get("name") if isinstance(function, dict) else None
        return str(tool.get("name") or function_name or "")
    return str(getattr(tool, "name", ""))


class SurfaceGuard(AgentMiddleware[Any, Any, Any]):
    def __init__(self, policy: ToolSurfacePolicy, audit: AuditWriter | None = None) -> None:
        super().__init__()
        self.policy = policy
        self.tools: list[Any] = []
        self.audit = audit

    def _filter(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        return request.override(tools=[tool for tool in request.tools if self.policy.permits(_tool_name(tool))])

    def _denial(self, request: ToolCallRequest) -> ToolMessage | None:
        name = str(request.tool_call["name"])
        args = request.tool_call.get("args") or {}
        if self.policy.permits_call(name, args if isinstance(args, dict) else {}):
            return None
        if self.audit is not None:
            self.audit.record(
                "surface",
                name,
                state=request.state,
                decision="block",
                detail={"tool_call_id": request.tool_call.get("id"), "args": args},
            )
        return ToolMessage(
            content=f"Tool {name!r} is not allowed for this agent.",
            tool_call_id=request.tool_call.get("id") or "",
            name=name,
            status="error",
        )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._filter(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._denial(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return self._denial(request) or await handler(request)
