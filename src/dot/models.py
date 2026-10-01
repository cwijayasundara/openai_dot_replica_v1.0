"""Chat model factory. Fireworks, or any OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_fireworks import ChatFireworks
from langchain_openai import ChatOpenAI

from .config import Role, Settings, get_settings


class ReasoningChatFireworks(ChatFireworks):
    """``ChatFireworks`` that sends each assistant turn's reasoning back.

    langchain-fireworks 1.7 reads ``reasoning_content`` from responses but
    leaves it off assistant messages in later requests. Its stream parser also
    drops reasoning deltas. Kimi K3 stops reasoning after the first tool turn
    when its history is stripped (design 4.4), so requests re-attach it, and
    generation does not stream, so reasoning is always captured.
    """

    disable_streaming: bool | Literal["tool_calling"] = True

    def _create_message_dicts(
        self, messages: list[BaseMessage], stop: list[str] | None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        message_dicts, params = super()._create_message_dicts(messages, stop)
        for message, payload in zip(messages, message_dicts, strict=True):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                payload["reasoning_content"] = reasoning
        return message_dicts, params


def chat_model(role: Role, settings: Settings | None = None) -> BaseChatModel:
    settings = settings or get_settings()
    name = settings.model_name(role)
    common: dict[str, object] = {
        "model": name,
        "timeout": settings.model_timeout_s,
        "max_retries": settings.model_max_retries,
        "api_key": settings.fireworks_api_key,
    }
    if settings.model_provider == "fireworks":
        return ReasoningChatFireworks(**common)  # type: ignore[arg-type]
    return ChatOpenAI(base_url=settings.openai_base_url, **common)  # type: ignore[arg-type]
