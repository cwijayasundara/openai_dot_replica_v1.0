"""Scrub secret values from what a model reads and what tools return."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from threading import RLock
from typing import Any, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

MASK = "[REDACTED]"
MessageT = TypeVar("MessageT", bound=BaseMessage)
PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)(\s*[:=]\s*)[^\s,;]+"),
)


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = RLock()
        self.secrets: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str) -> None:
        """Resolved values are registered before tool code receives them."""
        if secret:
            with self._lock:
                self.secrets = sorted({*self.secrets, secret}, key=len, reverse=True)

    def text(self, value: str) -> str:
        with self._lock:
            secrets = list(self.secrets)
        for secret in secrets:
            value = value.replace(secret, MASK)
            value = value.replace(json.dumps(secret, ensure_ascii=True)[1:-1], MASK)
        for pattern in PATTERNS:
            value = pattern.sub(_mask_match, value)
        return value

    def content(self, content: Any) -> Any:
        if isinstance(content, str):
            return self.text(content)
        if isinstance(content, list):
            return [self.content(block) for block in content]
        if isinstance(content, dict):
            return {
                self.text(key) if isinstance(key, str) else key: self.content(value) for key, value in content.items()
            }
        if isinstance(content, tuple):
            return tuple(self.content(value) for value in content)
        return content

    def message(self, message: MessageT) -> MessageT:
        fields = ("content", "additional_kwargs", "response_metadata", "artifact", "tool_calls", "invalid_tool_calls")
        updates = {name: self.content(getattr(message, name)) for name in fields if hasattr(message, name)}
        return message.model_copy(update=updates)


def _mask_match(match: re.Match[str]) -> str:
    if match.re.groups == 2:
        return f"{match.group(1)}{match.group(2)}{MASK}"
    return MASK


class RedactionMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, secrets: Iterable[str] = (), *, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor if redactor is not None else Redactor(secrets)
        self.tools: list[Any] = []

    def _request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        messages: list[Any] = [self.redactor.message(message) for message in request.messages]
        system = self.redactor.message(request.system_message) if request.system_message is not None else None
        return request.override(messages=messages, system_message=system)

    def _response(self, response: ModelResponse[Any]) -> ModelResponse[Any]:
        response.result = [self.redactor.message(message) for message in response.result]
        return response

    def _tool(self, result: ToolMessage | Command[Any]) -> ToolMessage | Command[Any]:
        if isinstance(result, ToolMessage):
            cleaned = self.redactor.message(result)
            if isinstance(cleaned, ToolMessage):
                return cleaned
        if isinstance(result, Command) and isinstance(result.update, dict):
            update = dict(result.update)
            if isinstance(update.get("messages"), list):
                update["messages"] = [
                    self.redactor.message(m) if isinstance(m, BaseMessage) else self.redactor.content(m)
                    for m in update["messages"]
                ]
            return Command(graph=result.graph, update=update, resume=result.resume, goto=result.goto)
        return result

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return self._response(handler(self._request(request)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return self._response(await handler(self._request(request)))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        try:
            return self._tool(handler(request))
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return self._failure(request, exc)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        try:
            return self._tool(await handler(request))
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return self._failure(request, exc)

    @staticmethod
    def _failure(request: ToolCallRequest, exc: Exception) -> ToolMessage:
        # Provider/transport exception text may contain credentials. Do not
        # propagate it into graph state or a worker's outward error event.
        return ToolMessage(
            content=f"Tool failed ({type(exc).__name__}).",
            status="error",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call.get("id") or "",
        )
