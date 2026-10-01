"""The channel seam. Inbound messages become inbox rows; outbound events become outbox rows.

A channel adapter normalises what arrives into ``Inbound`` and posts what the
outbox holds to ``reply_ref``, the channel's own address for the conversation
(for Slack, a channel id and thread timestamp). The worker never calls a
channel: it tags each inbound message with its reply channel, and
``DeliveringEventChannel`` turns assistant replies and approval cards for that
channel into outbox rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from dot.channels.outbox import Outbox
from dot.persistence.db import InboxMessage, Json, Repositories
from dot.runtime.router import Source, enqueue
from dot.runtime.turns import EventChannel, TurnEvent


@dataclass(frozen=True)
class Inbound:
    """One message from an authorized sender, already routed to its dot."""

    source: Source
    user: str
    dot_id: str
    text: str
    reply_ref: Json


def accept(repos: Repositories, inbound: Inbound, profile: str = "chat") -> InboxMessage:
    payload: Json = {"text": inbound.text, "user": inbound.user, "reply_ref": dict(inbound.reply_ref)}
    return enqueue(repos, inbound.dot_id, inbound.source, payload, profile)


def reply_ref(channel: object, source: str) -> Json | None:
    """The address a tagged reply goes to, if it belongs to ``source``."""
    if not isinstance(channel, dict) or channel.get("source") != source:
        return None
    ref = channel.get("reply_ref")
    return dict(ref) if isinstance(ref, dict) else None


class DeliveringEventChannel:
    """Publishes as usual, and queues replies and new approval cards for external channels."""

    def __init__(self, inner: EventChannel, outbox: Outbox, channels: Iterable[str]) -> None:
        self._inner = inner
        self._outbox = outbox
        self._channels = frozenset(channels)

    def publish(self, event: TurnEvent) -> None:
        self._inner.publish(event)
        kind = _deliverable(event)
        if kind is None:
            return
        channel = event.detail.get("channel")
        for name in self._channels:
            target = reply_ref(channel, name)
            if target is not None:
                self._outbox.add(event.dot_id, name, kind, target, dict(event.detail))


def _deliverable(event: TurnEvent) -> str | None:
    if event.kind == "message" and event.detail.get("role") == "assistant":
        return "message"
    if event.kind == "approval" and event.detail.get("status") == "pending" and "tool" in event.detail:
        return "approval"
    return None
