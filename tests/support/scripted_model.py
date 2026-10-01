"""A chat model that replays a script of tool calls. Graph behaviour is tested with this, never a live model."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ConfigDict, Field

Step = AIMessage | Callable[[list[BaseMessage]], AIMessage]
_ids = itertools.count(1)


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def tools(*calls: dict[str, Any], text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls))


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


def _names(tools: Sequence[Any]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, dict):
            function = tool.get("function")
            function_name = function.get("name") if isinstance(function, dict) else None
            names.append(str(tool.get("name") or function_name or "?"))
        else:
            names.append(str(getattr(tool, "name", "?")))
    return names


class ScriptedChatModel(BaseChatModel):
    """Returns the scripted steps in order and records what it was offered."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[Any] = Field(default_factory=list)
    position: int = 0
    offered: list[list[str]] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    requests: list[list[Any]] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)
    structured_script: list[Any] = Field(default_factory=list)
    structured_seen: list[list[BaseMessage]] = Field(default_factory=list)

    def with_structured_output(
        self,
        schema: Any,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Independent scripted review; it never consumes the agent's tool script."""
        del include_raw, kwargs

        def respond(messages: list[BaseMessage]) -> Any:
            index = len(self.structured_seen)
            self.structured_seen.append(list(messages))
            if self.structured_script:
                if index >= len(self.structured_script):
                    raise AssertionError("structured script exhausted")
                value = self.structured_script[index]
                if isinstance(value, Exception):
                    raise value
            else:
                value = {"in_scope": True, "risk": "low", "reason": "Allowed by the scripted reviewer."}
            return schema.model_validate(value)

        return RunnableLambda(respond)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def calls(self) -> int:
        return self.position

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:  # type: ignore[override]
        del kwargs
        self.bound_tools = _names(tools)
        self.requests.append(list(tools))
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        if self.position >= len(self.script):
            raise AssertionError(f"scripted model ran out of steps after {self.position} calls")
        step = self.script[self.position]
        self.position += 1
        runtime_tools = kwargs.get("tools")
        if isinstance(runtime_tools, list):
            self.offered.append(_names(runtime_tools))
        else:
            self.offered.append(list(self.bound_tools))
        self.seen.append(list(messages))
        message = step(messages) if callable(step) else step
        # A fresh copy each time: LangGraph mutates ids on messages it stores.
        message = message.model_copy(deep=True)
        return ChatResult(generations=[ChatGeneration(message=message)])
