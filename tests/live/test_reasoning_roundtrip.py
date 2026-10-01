"""Reasoning content must survive a three-tool loop in the checkpointed history.

Kimi K3 degrades when reasoning is dropped between tool turns. This is the gate
in design section 4.4: if it fails, fix the model integration before later phases.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from dot.config import HEAVY_MODEL, SUPERVISOR_MODEL, get_settings
from dot.models import chat_model

pytestmark = pytest.mark.live


@tool
def step_one(n: int) -> str:
    """First step. Pass its result to step_two."""
    return str(n + 1)


@tool
def step_two(n: int) -> str:
    """Second step. Pass its result to step_three."""
    return str(n + 1)


@tool
def step_three(n: int) -> str:
    """Third step. Return this number as the final answer."""
    return str(n + 1)


def _has_reasoning(message: AIMessage) -> bool:
    extra = message.additional_kwargs or {}
    if extra.get("reasoning_content"):
        return True
    content: Any = message.content
    if isinstance(content, list):
        return any(isinstance(block, dict) and block.get("type") in {"reasoning", "thinking"} for block in content)
    return False


@pytest.mark.parametrize("model_id", [HEAVY_MODEL, SUPERVISOR_MODEL])
def test_reasoning_survives_a_three_tool_loop(model_id: str) -> None:
    from deepagents import create_deep_agent

    settings = get_settings().model_copy(update={"heavy_model": model_id})
    model = chat_model("heavy", settings)
    sent: list[list[dict[str, Any]]] = []
    original = model._create_message_dicts  # type: ignore[attr-defined]

    def record(messages: list[Any], stop: list[str] | None) -> Any:
        payload, params = original(messages, stop)
        sent.append(payload)
        return payload, params

    object.__setattr__(model, "_create_message_dicts", record)
    checkpointer = MemorySaver()
    agent = create_deep_agent(
        model=model,
        tools=[step_one, step_two, step_three],
        system_prompt=(
            "Call step_one with 1, then step_two with that result, then step_three with that result. "
            "Answer with the number step_three returns."
        ),
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    agent.invoke(
        {"messages": [{"role": "user", "content": "Run the three steps, starting from 1."}]},
        config=config,
    )
    state = agent.get_state(config)
    assistants = [message for message in state.values["messages"] if isinstance(message, AIMessage)]
    assert len(assistants) >= 3, assistants
    assert _has_reasoning(assistants[0]), assistants[0]
    # The integration gate: every reasoning trace produced goes back in the next request.
    resent = {m.get("reasoning_content") for request in sent for m in request if m.get("role") == "assistant"}
    for message in assistants[:-1]:
        if _has_reasoning(message):
            assert message.additional_kwargs["reasoning_content"] in resent
    if model_id == HEAVY_MODEL:
        # Kimi K3 stops reasoning once its history is stripped; with it intact it reasons every turn.
        # GLM-5.3 skips reasoning on trivial steps even with full history (seen on the raw API).
        missing = [message for message in assistants if not _has_reasoning(message)]
        assert not missing, missing
