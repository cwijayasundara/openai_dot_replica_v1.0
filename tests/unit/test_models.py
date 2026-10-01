from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_fireworks import ChatFireworks
from langchain_openai import ChatOpenAI

from dot.config import FAST_MODEL, HEAVY_MODEL, Settings
from dot.models import ReasoningChatFireworks, chat_model


def test_fireworks_model_for_each_role() -> None:
    settings = Settings(_env_file=None, fireworks_api_key="fw-test")  # type: ignore[call-arg]
    heavy = chat_model("heavy", settings)
    fast = chat_model("fast", settings)
    assert isinstance(heavy, ChatFireworks)
    assert isinstance(fast, ChatFireworks)
    assert heavy.model_name == HEAVY_MODEL
    assert fast.model_name == FAST_MODEL
    assert heavy.max_retries == settings.model_max_retries
    assert heavy.request_timeout == settings.model_timeout_s


def test_openai_compatible_model() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        model_provider="openai_compatible",
        openai_base_url="https://example.test/v1",
        fireworks_api_key="fw-test",
    )
    model = chat_model("supervisor", settings)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == settings.supervisor_model
    assert str(model.openai_api_base).rstrip("/") == "https://example.test/v1"
    assert model.max_retries == settings.model_max_retries
    assert model.request_timeout == settings.model_timeout_s


def test_unknown_role_rejected() -> None:
    settings = Settings(_env_file=None, fireworks_api_key="fw-test")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="role"):
        chat_model("critic", settings)  # type: ignore[arg-type]


def test_fireworks_requests_carry_reasoning_history() -> None:
    settings = Settings(_env_file=None, fireworks_api_key="fw-test")  # type: ignore[call-arg]
    model = chat_model("heavy", settings)
    assert isinstance(model, ReasoningChatFireworks) and model.disable_streaming is True
    history = [
        HumanMessage("Run the steps."),
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "Start with step one."},
            tool_calls=[{"name": "step_one", "args": {"n": 1}, "id": "c1", "type": "tool_call"}],
        ),
        ToolMessage(content="2", tool_call_id="c1"),
        AIMessage(content="Done."),
    ]
    payload, _ = model._create_message_dicts(history, None)
    assert payload[1]["reasoning_content"] == "Start with step one."
    assert payload[1]["tool_calls"][0]["function"]["name"] == "step_one"
    assert "reasoning_content" not in payload[0] and "reasoning_content" not in payload[3]
