"""Turn events for the web UI (SSE) and Slack.

The worker publishes here. An in-process channel is what tests and a
same-process subscriber read. ``PgEventChannel`` delivers the same events to
other processes with ``NOTIFY dot_events``.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import AIMessage, ToolMessage
from psycopg_pool import ConnectionPool

from dot.persistence.db import Json

EventKind = Literal["message", "tool_call", "interrupt", "approval", "job_started", "job_finished", "memory", "error"]


@dataclass(frozen=True)
class TurnEvent:
    dot_id: str
    kind: EventKind
    detail: Json


class EventChannel(Protocol):
    def publish(self, event: TurnEvent) -> None: ...


class InMemoryEventChannel:
    def __init__(self) -> None:
        self._events: list[TurnEvent] = []
        self._subscribers: list[queue.Queue[TurnEvent]] = []
        self._lock = threading.Lock()

    @property
    def events(self) -> list[TurnEvent]:
        with self._lock:
            return list(self._events)

    def publish(self, event: TurnEvent) -> None:
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def subscribe(self) -> queue.Queue[TurnEvent]:
        """Queue of events published from now on, prefixed with those already stored."""
        subscriber: queue.Queue[TurnEvent] = queue.Queue()
        with self._lock:
            for event in self._events:
                subscriber.put(event)
            self._subscribers.append(subscriber)
        return subscriber


class PgEventChannel:
    """``NOTIFY dot_events`` with a JSON payload. Listeners do not share this process."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def publish(self, event: TurnEvent) -> None:
        payload = json.dumps(
            {"dot_id": event.dot_id, "kind": event.kind, "detail": event.detail},
            default=str,
        )
        if len(payload) > 7000:
            payload = json.dumps({"dot_id": event.dot_id, "kind": event.kind, "detail": {"truncated": True}})
        with self._pool.connection() as conn:
            conn.execute("SELECT pg_notify('dot_events', %s)", (payload,))


def publish_graph_update(
    dot_id: str, update: Mapping[str, Any], channel: EventChannel, *, reply_to: Json | None = None
) -> None:
    """Translate one LangGraph ``updates`` chunk into turn events.

    ``reply_to`` is the inbound channel the turn answers; assistant messages
    carry it so a channel adapter posts them where the request was made.
    """
    if "__interrupt__" in update:
        raw = update["__interrupt__"]
        items = raw if isinstance(raw, (list, tuple)) else (raw,)
        for item in items:
            channel.publish(TurnEvent(dot_id, "interrupt", _interrupt_detail(item)))
    for key, payload in update.items():
        if key == "__interrupt__":
            continue
        for message in _messages(payload):
            _publish_message(dot_id, message, channel, reply_to)


def _messages(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("messages")
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    return [raw]


def _publish_message(dot_id: str, message: Any, channel: EventChannel, reply_to: Json | None) -> None:
    if isinstance(message, ToolMessage):
        _publish_job_started(dot_id, message, channel)
        return
    if not isinstance(message, AIMessage):
        return
    calls = list(message.tool_calls)
    text = _text(message.content).strip()
    if text and not calls:
        said: Json = {"role": "assistant", "text": text}
        if reply_to is not None:
            said["channel"] = reply_to
        channel.publish(TurnEvent(dot_id, "message", said))
    for call in calls:
        name = str(call.get("name", ""))
        args = call.get("args")
        detail: Json = {"name": name, "args": args if isinstance(args, dict) else {}}
        call_id = call.get("id")
        if isinstance(call_id, str):
            detail["id"] = call_id
        channel.publish(TurnEvent(dot_id, "tool_call", detail))


def _publish_job_started(dot_id: str, message: ToolMessage, channel: EventChannel) -> None:
    """Only a job that was actually created, so the UI can match ``job_finished``."""
    if message.name != "start_job" or message.status == "error":
        return
    try:
        result = json.loads(_text(message.content))
    except json.JSONDecodeError:
        return
    if isinstance(result, dict) and result.get("ok") is True and isinstance(result.get("job_id"), str):
        channel.publish(TurnEvent(dot_id, "job_started", {"job_id": result["job_id"], "status": result.get("status")}))


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _interrupt_detail(item: Any) -> Json:
    value = getattr(item, "value", item)
    if isinstance(value, dict):
        return {"value": value}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return {"value": value}
    return {"value": str(value)}
