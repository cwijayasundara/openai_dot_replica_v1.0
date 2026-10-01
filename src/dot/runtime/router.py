"""Channel router. Surfaces call ``enqueue`` and never run an agent."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from dot.persistence.db import InboxMessage, Json, Repositories

# ``job_result`` rows are written only by ``JobStore.finish``: their origin sets
# the Guardian's objective for the turn, so no surface may enqueue one.
Source = Literal["web", "slack", "schedule"]
SOURCES: frozenset[str] = frozenset({"web", "slack", "schedule"})
# Set by the worker on each inbound message: where a reply to it belongs.
CHANNEL_KEY = "dot_channel"


def enqueue(
    repos: Repositories,
    dot_id: str,
    source: Source,
    payload: Mapping[str, Any],
    profile: str,
) -> InboxMessage:
    """Append one inbound message for a dot. The worker claims it later."""
    if source not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        raise ValueError(f"unknown inbox source {source!r} (known: {known})")
    if not profile:
        raise ValueError("profile is required")
    return repos.insert_inbox(
        InboxMessage(
            id=0,
            dot_id=dot_id,
            source=source,
            payload=dict(payload),
            profile=profile,
            created_at=datetime.now(UTC),
        )
    )


def render_inbound(message: InboxMessage) -> str:
    """Text the supervisor sees for one inbox row, with the channel named."""
    text = message.payload.get("text")
    body = text if isinstance(text, str) else json.dumps(message.payload, sort_keys=True, default=str)
    return f"[{message.source}] {body}"


def message_detail(message: InboxMessage) -> Json:
    return {
        "role": "user",
        "source": message.source,
        "inbox_id": message.id,
        "text": render_inbound(message),
    }


def latest_channel(messages: Sequence[Any]) -> Json | None:
    """The reply channel tagged on the most recent inbound message."""
    for message in reversed(messages):
        kwargs = getattr(message, "additional_kwargs", None) if getattr(message, "type", None) == "human" else None
        channel = kwargs.get(CHANNEL_KEY) if isinstance(kwargs, dict) else None
        if isinstance(channel, dict):
            return dict(channel)
    return None
