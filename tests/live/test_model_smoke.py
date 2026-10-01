"""Each role, behind a Deep Agent, must call a tool and answer. Needs a Fireworks key."""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from dot.config import Role, get_settings
from dot.models import chat_model

pytestmark = pytest.mark.live


@tool
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


@pytest.mark.parametrize("role", ["supervisor", "heavy", "fast"])
def test_role_calls_add(role: Role) -> None:
    from deepagents import create_deep_agent

    agent = create_deep_agent(
        model=chat_model(role, get_settings()),
        tools=[add],
        system_prompt="You answer arithmetic by calling the add tool. Do not estimate.",
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "What is 19 + 23? Call add."}]})
    messages = result["messages"]
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert tool_messages, messages
    assert any("42" in str(message.content) for message in messages)
