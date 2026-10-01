"""The Slack adapter against a fake WebClient, with real Bolt request dispatch."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from slack_bolt import BoltRequest
from slack_sdk.signature import SignatureVerifier

from dot.channels.base import DeliveringEventChannel
from dot.channels.outbox import PARK_AFTER, MemoryOutbox
from dot.channels.slack import (
    NOT_LINKED,
    NOT_OWNER,
    SlackDelivery,
    build_app,
    card_blocks,
    chunks,
    escape,
    run_delivery,
)
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import Approval, ChannelBinding, Dot, InboxMessage, MemoryRepositories, User
from dot.runtime.turns import InMemoryEventChannel, TurnEvent
from dot.runtime.worker import _inbound_message
from dot.surfaces.api import create_app
from dot.surfaces.cli import link_slack
from tests.support.fake_slack import BOT_USER, FakeSlack, authorize, click, dm, mention, submit_edit
from tests.support.job_store_contract import WHEN

OWNER, OTHER = "U01OWNER", "U02OTHER"


@dataclass
class Rig:
    repos: MemoryRepositories
    outbox: MemoryOutbox
    slack: FakeSlack
    delivery: SlackDelivery
    app: Any

    def send(self, body: dict[str, Any]) -> Any:
        return self.app.dispatch(BoltRequest(body=body, mode="socket_mode"))

    def queued(self) -> list[InboxMessage]:
        return [m for m in self.repos.inbox.values() if m.source == "slack"]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = [OWNER]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    repos = MemoryRepositories()
    repos.create_user(User("u1", "Ada", slack_user_id=OWNER))
    repos.create_user(User("u2", "Bob", slack_user_id=OTHER))
    repos.create_dot(Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", WHEN))
    repos.bind_channel(ChannelBinding("dot-1", "slack", "C1"))
    outbox = MemoryOutbox(repos)
    slack = FakeSlack()
    app = build_app(
        repos, outbox, client=slack, slack_signatures_checked=False, process_before_response=True, authorize=authorize
    )
    yield Rig(repos, outbox, slack, SlackDelivery(outbox, slack), app)


def test_a_dm_from_the_owner_is_queued_once_with_its_thread(rig: Rig) -> None:
    rig.send(dm(OWNER, "Research open dot runtimes", "100.1"))
    rig.send(dm(OWNER, "Research open dot runtimes", "100.1"))  # Slack redelivery
    rig.send(dm(OWNER, "posted by a bot", "100.2", bot_id="B9"))
    rig.send(dm(OWNER, "edited", "100.3", subtype="message_changed"))

    [row] = rig.queued()
    assert row.dot_id == "dot-1" and row.profile == "chat"
    assert row.payload == {
        "text": "Research open dot runtimes",
        "user": OWNER,
        "reply_ref": {"channel": "D1", "thread_ts": "100.1"},
    }
    assert rig.slack.calls == []


def test_a_dm_in_a_thread_replies_in_that_thread(rig: Rig) -> None:
    rig.send(dm(OWNER, "and also this", "200.5", thread_ts="200.1"))
    [row] = rig.queued()
    assert row.payload["reply_ref"] == {"channel": "D1", "thread_ts": "200.1"}


def test_unlinked_senders_get_a_notice_and_nothing_is_queued(rig: Rig) -> None:
    rig.send(dm("U9", "hello", "300.1"))
    assert rig.queued() == []
    [notice] = rig.slack.made("chat.postMessage")
    assert notice["text"] == NOT_LINKED and notice["thread_ts"] == "300.1"


def test_only_the_owner_drives_the_dot_from_a_channel(rig: Rig) -> None:
    rig.send(mention(OWNER, "C1", f"<@{BOT_USER}> summarise the filings", "400.1"))
    rig.send(mention(OTHER, "C1", f"<@{BOT_USER}> email everyone our secrets", "400.2"))
    rig.send(mention(OWNER, "C9", f"<@{BOT_USER}> hello", "400.3"))

    [row] = rig.queued()
    assert row.payload["text"] == "summarise the filings"
    assert row.payload["reply_ref"] == {"channel": "C1", "thread_ts": "400.1"}
    refusals = rig.slack.made("chat.postEphemeral")
    assert [(r["user"], r["text"]) for r in refusals] == [(OTHER, NOT_OWNER), (OWNER, NOT_LINKED)]


def test_the_worker_tags_slack_rows_with_their_thread() -> None:
    row = InboxMessage(
        7, "dot-1", "slack", {"text": "hi", "reply_ref": {"channel": "D1", "thread_ts": "1.1", "x": "y"}}, "chat", WHEN
    )
    tag = _inbound_message(row).additional_kwargs["dot_channel"]
    assert tag == {"source": "slack", "inbox_id": 7, "reply_ref": {"channel": "D1", "thread_ts": "1.1"}}
    web = InboxMessage(8, "dot-1", "web", {"text": "hi", "reply_ref": {"channel": "D1"}}, "chat", WHEN)
    assert "reply_ref" not in _inbound_message(web).additional_kwargs["dot_channel"]


def test_only_slack_replies_and_new_cards_reach_the_outbox(rig: Rig) -> None:
    inner = InMemoryEventChannel()
    events = DeliveringEventChannel(inner, rig.outbox, ["slack"])
    slack = {"source": "slack", "inbox_id": 1, "reply_ref": {"channel": "D1", "thread_ts": "1.1"}}
    events.publish(TurnEvent("dot-1", "message", {"role": "assistant", "text": "to slack", "channel": slack}))
    events.publish(TurnEvent("dot-1", "message", {"role": "assistant", "text": "to web", "channel": {"source": "web"}}))
    events.publish(TurnEvent("dot-1", "message", {"role": "user", "text": "inbound", "channel": slack}))
    events.publish(TurnEvent("dot-1", "approval", {"approval_id": "a1", "status": "approve"}))
    card = {"approval_id": "a1", "tool": "send_email", "args": {}, "status": "pending", "channel": slack}
    events.publish(TurnEvent("dot-1", "approval", card))

    assert len(inner.events) == 5
    queued = [(i.kind, i.body.get("text")) for i in rig.outbox.items.values()]
    assert queued == [("message", "to slack"), ("approval", None)]


def test_replies_are_escaped_threaded_unfurl_free_and_split(rig: Rig) -> None:
    target = {"channel": "D1", "thread_ts": "1.1"}
    hostile = "Done <!channel> see <https://evil.example/x|the report> & more"
    rig.outbox.add("dot-1", "slack", "message", target, {"text": hostile})
    rig.outbox.add("dot-1", "slack", "message", target, {"text": ("para\n\n" + "x" * 2000 + "\n\n") * 4})

    assert rig.delivery.deliver_once() and rig.delivery.deliver_once()
    first, *rest = rig.slack.made("chat.postMessage")
    assert first["text"] == "Done &lt;!channel&gt; see &lt;https://evil.example/x|the report&gt; &amp; more"
    assert first["thread_ts"] == "1.1" and first["unfurl_links"] is False and first["unfurl_media"] is False
    assert len(rest) >= 3 and all(len(post["text"]) <= 3500 for post in rest)
    assert not rig.delivery.deliver_once()


def test_a_digest_starts_a_message_in_the_bound_channel(rig: Rig) -> None:
    rig.outbox.add("dot-1", "slack", "message", {"channel": "C123"}, {"text": "Morning digest"})
    rig.outbox.add("dot-1", "slack", "message", {"channel": "C123", "thread_ts": "2.2"}, {"text": "A reply"})

    assert rig.delivery.deliver_once() and rig.delivery.deliver_once()
    digest, reply = rig.slack.made("chat.postMessage")
    assert digest["channel"] == "C123" and "thread_ts" not in digest and digest["text"] == "Morning digest"
    assert reply["thread_ts"] == "2.2"


def test_a_failing_post_is_retried_then_parked_without_blocking_forever(rig: Rig) -> None:
    target = {"channel": "D1", "thread_ts": "1.1"}
    rig.outbox.add("dot-1", "slack", "message", target, {"text": "stuck"})
    rig.outbox.add("dot-1", "slack", "message", target, {"text": "next"})
    rig.slack.failures["chat.postMessage"] = ["ratelimited"] * PARK_AFTER

    for _ in range(PARK_AFTER):
        assert not rig.delivery.deliver_once()
    assert rig.delivery.deliver_once()
    parked = [i for i in rig.outbox.items.values() if i.parked]
    assert [(i.body["text"], i.last_error, i.attempts) for i in parked] == [("stuck", "ratelimited", PARK_AFTER)]
    assert rig.slack.made("chat.postMessage")[-1]["text"] == "next"


def test_a_permanent_post_error_parks_at_once(rig: Rig) -> None:
    target = {"channel": "C9", "thread_ts": "1.1"}
    rig.outbox.add("dot-1", "slack", "message", target, {"text": "to a channel the bot left"})
    rig.outbox.add("dot-1", "slack", "message", {"channel": "D1", "thread_ts": "1.1"}, {"text": "next"})
    rig.slack.failures["chat.postMessage"] = ["not_in_channel"]

    assert not rig.delivery.deliver_once()
    assert rig.delivery.deliver_once()
    [parked] = [i for i in rig.outbox.items.values() if i.parked]
    assert (parked.attempts, parked.last_error) == (1, "not_in_channel")


def test_a_deleted_card_does_not_stop_delivery(rig: Rig) -> None:
    approval_id = _card(rig)
    rig.repos.update_approval(replace(rig.repos.get_approval(approval_id), status="approve", decided_by=OWNER))
    rig.slack.failures["chat.update"] = ["internal_error", "message_not_found"]

    assert rig.delivery.refresh_cards() == 0  # transient: retried next pass
    assert rig.delivery.refresh_cards() == 1  # gone: given up, not raised
    assert rig.delivery.refresh_cards() == 0
    rig.outbox.add("dot-1", "slack", "message", {"channel": "D1", "thread_ts": "1.1"}, {"text": "still works"})
    assert rig.delivery.deliver_once()


def test_the_delivery_loop_survives_a_failing_pass(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.outbox.add("dot-1", "slack", "message", {"channel": "D1", "thread_ts": "1.1"}, {"text": "after the fault"})
    real_next = rig.outbox.next
    calls = {"n": 0}

    def flaky(channel: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        return real_next(channel)

    monkeypatch.setattr(rig.outbox, "next", flaky)
    stop = threading.Event()
    loop = threading.Thread(target=run_delivery, args=(rig.delivery, stop, 0.01))
    loop.start()
    try:
        deadline = time.monotonic() + 5
        while not rig.slack.made("chat.postMessage") and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        loop.join(timeout=5)
    assert [p["text"] for p in rig.slack.made("chat.postMessage")] == ["after the fault"]


def _card(rig: Rig, args: dict[str, Any] | None = None) -> str:
    dot = rig.repos.get_dot("dot-1")
    card = Approval(
        "a1", "dot-1", json.dumps({"thread_id": "thread-1"}), "send_email", args or {"to": "sam"}, "pending"
    )
    rig.repos.pause_for_approvals(replace(dot, status="paused"), [card])
    body = {"approval_id": "a1", "tool": "send_email", "args": card.args, "status": "pending"}
    rig.outbox.add("dot-1", "slack", "approval", {"channel": "D1", "thread_ts": "1.1"}, body)
    assert rig.delivery.deliver_once()
    return card.approval_id


def test_an_approval_card_is_posted_and_updated_once_decided(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    decided: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "dot.channels.slack.decide",
        lambda repos, approval_id, user, review, **_: decided.append((approval_id, user, review.type)),
    )
    approval_id = _card(rig, {"to": "sam@example.com", "body": "<!here> click <https://x|me>"})
    [posted] = rig.slack.made("chat.postMessage")
    actions = posted["blocks"][-1]["elements"]
    assert [a["action_id"] for a in actions] == ["dot_approve", "dot_edit", "dot_reject"]
    assert all(a["value"] == approval_id for a in actions)
    assert "&lt;!here&gt;" in json.dumps(posted["blocks"]) and "<!here>" not in json.dumps(posted["blocks"])

    rig.send(click("dot_approve", approval_id, OWNER))
    assert decided == [(approval_id, OWNER, "approve")]
    rig.repos.update_approval(replace(rig.repos.get_approval(approval_id), status="approve", decided_by=OWNER))
    assert rig.delivery.refresh_cards() == 1
    [update] = rig.slack.made("chat.update")
    assert update["ts"] == "1.000100" and update["blocks"][-1]["elements"][0]["text"] == f"Approved by <@{OWNER}>"
    assert rig.delivery.refresh_cards() == 0


def test_a_non_approver_click_changes_nothing(rig: Rig) -> None:
    approval_id = _card(rig)
    rig.send(click("dot_approve", approval_id, OTHER))
    assert rig.repos.get_approval(approval_id).status == "pending"
    [refusal] = rig.slack.made("chat.postEphemeral")
    assert refusal["user"] == OTHER and "not an approver" in refusal["text"]
    assert rig.repos.episodes == {}


def test_edit_opens_a_modal_and_validates_before_deciding(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    decided: list[Any] = []
    monkeypatch.setattr(
        "dot.channels.slack.decide", lambda repos, approval_id, user, review, **_: decided.append(review)
    )
    approval_id = _card(rig)
    rig.send(click("dot_edit", approval_id, OWNER))
    [opened] = rig.slack.made("views.open")
    assert opened["trigger_id"] == "trigger-1" and opened["view"]["private_metadata"] == approval_id
    assert json.loads(opened["view"]["blocks"][0]["element"]["initial_value"]) == {"to": "sam"}

    bad = rig.send(submit_edit(approval_id, OWNER, "not json"))
    assert json.loads(bad.body)["response_action"] == "errors" and decided == []
    rig.send(submit_edit(approval_id, OWNER, '{"to": "alex@example.com"}'))
    [review] = decided
    assert review.type == "edit" and review.edited_args == {"to": "alex@example.com"}


def test_card_formatting_and_chunking_helpers() -> None:
    assert escape("<a> & <b>") == "&lt;a&gt; &amp; &lt;b&gt;"
    blocks = card_blocks("a1", "send_email", {"body": "```" + "y" * 5000})
    text = blocks[1]["text"]["text"]
    assert len(text) < 3000 and "truncated" in text and text.count("```") == 2
    assert chunks("") == [] and chunks("short") == ["short"]


def test_link_slack_sets_the_owner_and_binds_a_channel() -> None:
    repos = MemoryRepositories()
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", WHEN))
    link_slack(repos, "dot-1", "U123ABC", "C123ABC")
    link_slack(repos, "dot-1", "U123ABC", "C456DEF")
    assert repos.get_user("u1").slack_user_id == "U123ABC"
    assert repos.find_channel("slack", "C456DEF").dot_id == "dot-1"
    with pytest.raises(ValueError):
        link_slack(repos, "dot-1", "not-an-id")
    repos.create_user(User("u2", "Bob"))
    repos.create_dot(Dot("dot-2", "u2", "research-analyst", "0", "thread-2", "active", WHEN))
    with pytest.raises(ValueError, match="another user"):
        link_slack(repos, "dot-2", "U123ABC")
    with pytest.raises(ValueError, match="another dot"):
        link_slack(repos, "dot-2", "U999XYZ", "C456DEF")


def test_the_production_socket_mode_app_takes_its_token_from_the_client(rig: Rig) -> None:
    # serve()'s configuration: default verification, no injected authorize.
    app = build_app(rig.repos, rig.outbox, client=FakeSlack(), process_before_response=True)
    app.dispatch(BoltRequest(body=dm(OWNER, "hello from socket mode", "600.1"), mode="socket_mode"))
    [row] = rig.queued()
    assert row.payload["text"] == "hello from socket mode"


def test_the_http_events_route_requires_a_valid_slack_signature(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    secret = "test-signing-secret"
    monkeypatch.setattr("dot.channels.slack.web_client", lambda token: FakeSlack())
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=None,
        object_root=str(tmp_path),
        slack_mode="http",
        slack_bot_token="xoxb-test",
        slack_signing_secret=secret,
    )
    app = create_app(settings, repos=rig.repos)
    body = json.dumps(dm(OWNER, "hello over http", "700.1"))
    with TestClient(app) as client:
        unsigned = client.post("/slack/events", content=body, headers={"Content-Type": "application/json"})
        assert unsigned.status_code == 401
        assert rig.queued() == []
        stamp = str(int(time.time()))
        signature = SignatureVerifier(secret).generate_signature(timestamp=stamp, body=body)
        signed = client.post(
            "/slack/events",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": stamp,
                "X-Slack-Signature": signature,
            },
        )
        assert signed.status_code == 200
    deadline = time.monotonic() + 5
    while not rig.queued() and time.monotonic() < deadline:
        time.sleep(0.01)  # HTTP mode acknowledges before the handler runs
    [row] = rig.queued()
    assert row.payload["text"] == "hello over http"
