"""One contract for the in-memory and Postgres outboxes."""

from __future__ import annotations

from dataclasses import replace

from dot.channels.outbox import PARK_AFTER, Outbox
from dot.persistence.db import Approval, Dot, Repositories, User
from tests.support.job_store_contract import WHEN


def exercise(outbox: Outbox, repos: Repositories) -> None:
    repos.create_user(User("u1", "Ada"))
    dot = Dot("d1", "u1", "research-analyst", "1", "thread-1", "active", WHEN)
    repos.create_dot(dot)
    target = {"channel": "D1", "thread_ts": "1.1"}

    assert outbox.next("slack") is None
    outbox.add("d1", "slack", "message", target, {"text": "first"})
    outbox.add("d1", "slack", "message", target, {"text": "second"})
    outbox.add("d1", "web", "message", target, {"text": "elsewhere"})
    head = outbox.next("slack")
    assert head is not None and head.body == {"text": "first"} and head.target == target

    # A failing head holds back later rows until it is parked.
    for attempt in range(1, PARK_AFTER):
        assert outbox.failed(head.id, "rate_limited") is False
        again = outbox.next("slack")
        assert again is not None and again.id == head.id and again.attempts == attempt
    assert outbox.failed(head.id, "rate_limited") is True
    second = outbox.next("slack")
    assert second is not None and second.body == {"text": "second"}
    outbox.delivered(second.id)
    assert outbox.next("slack") is None

    assert outbox.first_seen("slack", "D1:1.1") is True
    assert outbox.first_seen("slack", "D1:1.1") is False
    assert outbox.first_seen("slack", "D1:1.2") is True

    card = Approval("a1", "d1", "run-1", "send_email", {"to": "sam"}, "pending")
    repos.pause_for_approvals(replace(dot, status="paused"), [card])
    outbox.record_card("a1", "slack", {"channel": "D1", "ts": "9.9", "tool": "send_email", "args": {"to": "sam"}})
    assert outbox.card_changes("slack") == []
    repos.update_approval(replace(card, status="approve", decided_by="U1"))
    [change] = outbox.card_changes("slack")
    assert (change.approval_id, change.status, change.decided_by, change.ref["ts"]) == ("a1", "approve", "U1", "9.9")
    outbox.card_shown("a1", "slack", "approve")
    assert outbox.card_changes("slack") == []
