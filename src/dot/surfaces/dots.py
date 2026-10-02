"""Create a dot, queue a message, and read its thread.

The API and the CLI share this. Neither path is a second agent assembly:
``post_message`` only writes the inbox, and ``read_thread`` uses ``build_dot_agent``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from dot.assembly import GraphRuntime, build_dot_agent
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack, seed_store
from dot.persistence.db import Dot, InboxMessage, Json, NotFound, Repositories, User
from dot.runtime.router import CHANNEL_KEY, enqueue

PACK_VERSION = "0"


def create_dot(
    repos: Repositories,
    runtime: GraphRuntime,
    pack_name: str,
    owner_user_id: str,
    display_name: str | None = None,
) -> Dot:
    """Persist a dot and seed its skills and wiki. The caller runs no agent."""
    if not owner_user_id:
        raise ValueError("owner_user_id is required")
    loaded = load_pack(REPO_ROOT / "packs" / pack_name)
    _ensure_user(repos, owner_user_id, display_name or owner_user_id)
    dot_id = f"dot-{uuid4().hex[:16]}"
    dot = Dot(
        dot_id=dot_id,
        owner_user_id=owner_user_id,
        pack_name=loaded.pack.name,
        pack_version=PACK_VERSION,
        thread_id=dot_id,
        status="active",
        created_at=datetime.now(UTC),
    )
    repos.create_dot(dot)
    seed_store(loaded, runtime.store, dot_id)
    return dot


def post_message(repos: Repositories, dot_id: str, text: str, profile: str = "chat") -> InboxMessage:
    """Queue one web message. The worker runs it."""
    if not text:
        raise ValueError("text is required")
    dot = repos.get_dot(dot_id)
    loaded = load_pack(REPO_ROOT / "packs" / dot.pack_name)
    if profile not in loaded.pack.profiles:
        known = ", ".join(sorted(loaded.pack.profiles))
        raise ValueError(f"pack {dot.pack_name!r} has no profile {profile!r} (known: {known})")
    return enqueue(repos, dot_id, "web", {"text": text}, profile)


def read_thread(
    dot: Dot,
    settings: Settings,
    runtime: GraphRuntime,
    model: BaseChatModel | None = None,
) -> list[Json]:
    """Messages checkpointed on ``dot.thread_id``. Building the agent does not invoke it."""
    agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model)
    state = agent.get_state({"configurable": {"thread_id": dot.thread_id}})
    raw = state.values.get("messages") if isinstance(state.values, dict) else None
    messages = raw if isinstance(raw, list) else []
    return [_message_view(message) for message in messages]


def dot_view(dot: Dot) -> Json:
    return {
        "dot_id": dot.dot_id,
        "owner_user_id": dot.owner_user_id,
        "pack_name": dot.pack_name,
        "pack_version": dot.pack_version,
        "thread_id": dot.thread_id,
        "status": dot.status,
        "created_at": dot.created_at.isoformat(),
    }


def last_assistant_text(messages: list[Json]) -> str:
    for message in reversed(messages):
        if message.get("role") == "ai" and isinstance(message.get("content"), str) and str(message["content"]).strip():
            return str(message["content"])
    return ""


def _ensure_user(repos: Repositories, user_id: str, display_name: str) -> None:
    try:
        repos.get_user(user_id)
    except NotFound:
        repos.create_user(User(user_id, display_name))


def _message_view(message: Any) -> Json:
    if not isinstance(message, BaseMessage):
        return {"role": "unknown", "content": str(message)}
    view: Json = {"role": message.type, "content": _text(message.content)}
    if message.type == "ai" and message.id:
        # A correction names the AI message it corrects.
        view["id"] = message.id
    channel = message.additional_kwargs.get(CHANNEL_KEY)
    if isinstance(channel, dict) and isinstance(channel.get("source"), str):
        view["source"] = channel["source"]
    name = getattr(message, "name", None)
    if isinstance(name, str) and name:
        view["name"] = name
    calls = getattr(message, "tool_calls", None)
    if isinstance(calls, list) and calls:
        names = [str(call.get("name", "")) for call in calls if isinstance(call, dict)]
        if names:
            view["tool_calls"] = names
    return view


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
    return "".join(parts)
