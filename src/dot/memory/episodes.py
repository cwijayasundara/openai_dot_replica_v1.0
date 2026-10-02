"""Episodes: one human judgement on one dot proposal, and the graph state replay re-makes it from.

Approval episodes are written by ``safety.approvals.decide``. Corrections are
written here, only from an explicit human action against one of the dot's
messages; code never guesses which chat messages are corrections.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field

from dot.persistence.db import Dot, Episode, Json, NotFound, Repositories

# The worker tags every run's config with this; LangGraph copies it onto each checkpoint.
PROFILE_METADATA_KEY = "dot_profile"
# Bounds the search for a corrected message in a long thread.
CORRECTION_SCAN_LIMIT = 2000


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2000)


class StateGraph(Protocol):
    """The read side of a compiled graph. Messages are stored as deltas, so reads go through the graph."""

    def get_state(self, config: RunnableConfig) -> Any: ...
    def get_state_history(self, config: RunnableConfig, *, limit: int | None = None) -> Any: ...


@dataclass(frozen=True)
class ReplayPoint:
    """The dot's state just before it made the judged proposal."""

    episode_id: int
    thread_id: str
    profile: str
    checkpoint_id: str
    messages: list[BaseMessage]
    proposal: AIMessage
    calls: list[Json]


@dataclass(frozen=True)
class Unreplayable:
    episode_id: int
    reason: str


def record_correction(repos: Repositories, graph: StateGraph, dot: Dot, user_id: str, body: CorrectionBody) -> Episode:
    """Store a correction against one of the dot's own AI messages. NotFound when it is not one."""
    found = _find_message(graph, dot.thread_id, body.message_id)
    if found is None:
        raise NotFound("messages", body.message_id)
    checkpoint_id, values, metadata = found
    message = values["messages"][-1]
    profile = metadata.get(PROFILE_METADATA_KEY)
    proposal: Json = {
        "thread_id": dot.thread_id,
        "profile": profile if isinstance(profile, str) else None,
        "checkpoint_id": checkpoint_id,
        "message_id": body.message_id,
        "tool_calls": [{"name": call["name"], "args": call["args"]} for call in message.tool_calls],
    }
    episode = Episode(
        0,
        dot.dot_id,
        datetime.now(UTC),
        str(values.get("guardian_instruction", "")),
        proposal,
        "correction",
        {"text": body.text, "by": user_id},
    )
    return repos.insert_episode(episode)


def replay_point(repos: Repositories, graph: StateGraph, dot: Dot, episode: Episode) -> ReplayPoint | Unreplayable:
    """Where replay restarts this episode, or why it cannot.

    Only proposals the dot's own supervisor made can be replayed: job agents and
    subagents do not load ``AGENTS.md`` or skills, so a memory edit cannot change them.
    """
    if episode.dot_id != dot.dot_id:
        raise ValueError("episode belongs to another dot")
    if episode.human_action == "correction":
        return _correction_point(graph, dot, episode)
    if episode.human_action in {"approve", "edit", "reject"}:
        return _approval_point(repos, graph, dot, episode)
    return Unreplayable(episode.id, f"unknown action {episode.human_action!r}")


def _approval_point(repos: Repositories, graph: StateGraph, dot: Dot, episode: Episode) -> ReplayPoint | Unreplayable:
    try:
        card = repos.get_approval(str(episode.proposal["approval_id"]))
    except (KeyError, NotFound):
        return Unreplayable(episode.id, "approval card missing")
    reference = json.loads(card.run_ref)
    if reference.get("job_id") is not None:
        return Unreplayable(episode.id, "job proposal")
    if reference.get("thread_id") != dot.thread_id:
        return Unreplayable(episode.id, "not the dot's thread")
    values = _values_at(graph, dot.thread_id, str(reference.get("checkpoint_id", "")))
    if values is None:
        return Unreplayable(episode.id, "checkpoint missing")
    proposal = values["messages"][-1]
    calls = [
        {"name": call["name"], "args": call["args"]}
        for call in proposal.tool_calls
        if call["name"] == card.tool and call["args"] == card.args
    ]
    if not calls:
        return Unreplayable(episode.id, "proposed by a subagent")
    return _point(episode, dot, str(reference["profile"]), str(reference["checkpoint_id"]), values, calls[:1])


def _correction_point(graph: StateGraph, dot: Dot, episode: Episode) -> ReplayPoint | Unreplayable:
    proposal = episode.proposal
    if proposal.get("thread_id") != dot.thread_id:
        return Unreplayable(episode.id, "not the dot's thread")
    if not isinstance(proposal.get("profile"), str):
        return Unreplayable(episode.id, "profile unknown")
    if not proposal.get("tool_calls"):
        return Unreplayable(episode.id, "no tool call")
    values = _values_at(graph, dot.thread_id, str(proposal.get("checkpoint_id", "")))
    if values is None or values["messages"][-1].id != proposal.get("message_id"):
        return Unreplayable(episode.id, "checkpoint missing")
    return _point(
        episode, dot, proposal["profile"], str(proposal["checkpoint_id"]), values, list(proposal["tool_calls"])
    )


def _point(
    episode: Episode, dot: Dot, profile: str, checkpoint_id: str, values: dict[str, Any], calls: list[Json]
) -> ReplayPoint:
    *before, proposal = values["messages"]
    return ReplayPoint(episode.id, dot.thread_id, profile, checkpoint_id, before, proposal, calls)


def _values_at(graph: StateGraph, thread_id: str, checkpoint_id: str) -> dict[str, Any] | None:
    """State values at a checkpoint whose last message is an AI message, else None."""
    if not checkpoint_id:
        return None
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}})
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    if snapshot.config.get("configurable", {}).get("checkpoint_id") != checkpoint_id:
        return None
    messages = values.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], AIMessage):
        return None
    return values


def _find_message(
    graph: StateGraph, thread_id: str, message_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """The earliest checkpoint whose last message is this AI message: the step right after the model made it."""
    found: tuple[str, dict[str, Any], dict[str, Any]] | None = None
    history = graph.get_state_history({"configurable": {"thread_id": thread_id}}, limit=CORRECTION_SCAN_LIMIT)
    for snapshot in history:  # newest first
        values = snapshot.values if isinstance(snapshot.values, dict) else {}
        messages = values.get("messages")
        last = messages[-1] if isinstance(messages, list) and messages else None
        if isinstance(last, AIMessage) and last.id == message_id:
            found = (snapshot.config["configurable"]["checkpoint_id"], values, dict(snapshot.metadata or {}))
        elif found is not None:
            break
    return found
