"""Record actual (including edited) tool attempts and compact outcomes."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from dot.safety.audit import AuditWriter


class AuditMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, audit: AuditWriter) -> None:
        super().__init__()
        self.audit = audit

    def _record(self, request: ToolCallRequest, phase: str, **extra: Any) -> None:
        self.audit.record(
            "tool_call",
            request.tool_call["name"],
            state=request.state,
            detail={
                "phase": phase,
                "tool_call_id": request.tool_call.get("id"),
                "args": request.tool_call["args"],
                **extra,
            },
        )

    def _result(self, request: ToolCallRequest, result: ToolMessage | Command[Any]) -> None:
        # Do not copy tool output into the audit log (it may be huge or private).
        status = result.status if isinstance(result, ToolMessage) else "command"
        if isinstance(result, ToolMessage) and isinstance(result.content, str):
            try:
                payload = json.loads(result.content)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and payload.get("ok") is False:
                status = "error"
        self._record(request, "result", status=status)

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]]
    ) -> ToolMessage | Command[Any]:
        self._record(request, "attempt")
        try:
            result = handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            self._record(request, "error", error_type=type(exc).__name__)
            raise
        self._result(request, result)
        return result

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        self._record(request, "attempt")
        try:
            result = await handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            self._record(request, "error", error_type=type(exc).__name__)
            raise
        self._result(request, result)
        return result
