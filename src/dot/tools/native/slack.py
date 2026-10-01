"""Post to Slack through an injected client. The token never enters the result."""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from dot.tools.native.deps import ToolDeps
from dot.tools.results import fail, ok

MAX_CHARS = 4_000


def build_slack_post(deps: ToolDeps) -> BaseTool:
    def slack_post(channel: str, text: str) -> str:
        """Post a message to a Slack channel. This does not return the bot token."""
        if not channel.strip() or not text.strip():
            return fail("channel and text are required")
        if len(text) > MAX_CHARS:
            return fail("message is too long")
        if deps.slack is None:
            return fail("slack is not configured")
        timestamp = deps.slack.post(channel=channel.strip(), text=text)
        return ok(channel=channel.strip(), ts=timestamp, char_count=len(text))

    return StructuredTool.from_function(slack_post, name="slack_post")
