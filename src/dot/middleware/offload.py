"""Large tool results go to the dot's object root; the model sees a stub with the artifact id."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

PREVIEW_CHARS = 200


class OffloadMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, artifact_dir: Path, *, max_chars: int = 4000) -> None:
        super().__init__()
        self.artifact_dir = artifact_dir
        self.max_chars = max_chars
        self.tools: list[Any] = []

    def _offload(self, result: ToolMessage | Command[Any]) -> ToolMessage | Command[Any]:
        if not isinstance(result, ToolMessage):
            return result
        text = result.content if isinstance(result.content, str) else str(result.content)
        if len(text) <= self.max_chars:
            return result
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = f"tool-{hashlib.sha256(text.encode()).hexdigest()[:16]}.txt"
        (self.artifact_dir / artifact_id).write_text(text, encoding="utf-8")
        stub = (
            f"Result was {len(text)} characters and is stored as artifact {artifact_id}. "
            f"Preview: {text[:PREVIEW_CHARS]!r}"
        )
        return result.model_copy(update={"content": stub})

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._offload(handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return self._offload(await handler(request))
