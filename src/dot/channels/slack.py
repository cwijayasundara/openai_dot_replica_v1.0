"""Slack: DMs and mentions reach the dot; replies and approval cards return in thread.

Inbound handlers only enqueue or record a decision, and acknowledge within
Slack's three seconds. Slack's request verification (Socket Mode, or signed
HTTP) is what vouches for a Slack user id; that id is the principal for the
approver check.

- A DM goes to the one dot its sender owns (``users.slack_user_id``).
- A mention goes to the dot bound to that channel (``channel_bindings``), and
  only from the dot's owner. Nobody else can drive the owner's dot or set the
  Guardian's objective.
- Bot posts, edits and other subtypes are ignored, and each event is accepted
  once, so the dot never answers itself or a redelivery.

Everything posted is escaped and posted with link unfurling off. Model output
cannot ping a channel, disguise a link, or make Slack fetch a URL.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import threading
from collections.abc import Callable
from typing import Any

from slack_bolt import Ack, App
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from dot.channels.base import Inbound, accept
from dot.channels.outbox import CardChange, Outbox, OutboxItem
from dot.middleware.redaction import Redactor
from dot.persistence.db import ApprovalConflict, Dot, Json, NotFound, Repositories
from dot.safety.approvals import ReviewDecision, decide

log = logging.getLogger(__name__)
CHANNEL = "slack"
# Errors a retry cannot fix for this item: its conversation, message or payload
# is unusable. Auth errors are not here: they affect every item, so delivery
# stalls and retries rather than parking the whole queue.
PERMANENT_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "message_not_found",
        "cant_update_message",
        "invalid_blocks",
        "msg_too_long",
        "no_text",
    }
)
DELIVERY_LOCK = "outbox:slack"
# Slack renders at most 3,000 characters in a section and modal input.
SECTION_CHARS = 2_900
MESSAGE_CHARS = 3_500
_MENTION = re.compile(r"<@[A-Z0-9]+>")
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,}")
_DECIDED = {"approve": "Approved", "edit": "Approved with edits", "reject": "Rejected", "cancelled": "Cancelled"}

NOT_LINKED = "This Slack account is not linked to a dot. Ask an operator to run `dot link-slack`."
NOT_OWNER = "Only this dot's owner can ask it to do things."


def escape(text: str) -> str:
    """Slack's control characters. ``<!channel>`` and ``<url|label>`` become plain text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chunks(text: str, size: int = MESSAGE_CHARS) -> list[str]:
    """Split on paragraph or line breaks where possible."""
    parts: list[str] = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size))
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*parts, text] if text else parts


def card_blocks(approval_id: str, tool: str, args: Any, outcome: str | None = None) -> list[Json]:
    shown = json.dumps(args, indent=2, sort_keys=True, default=str)
    if len(shown) > SECTION_CHARS:
        shown = shown[:SECTION_CHARS] + "\n… (truncated; see the web UI for the full arguments)"
    # A literal fence in the arguments would end the code block early.
    shown = escape(shown).replace("```", "`​``")
    blocks: list[Json] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Approval needed:* `{escape(tool)}`"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{shown}```"}},
    ]
    if outcome is None:
        blocks.append(
            {
                "type": "actions",
                "block_id": "dot_review",
                "elements": [
                    _button("Approve", "dot_approve", approval_id, "primary"),
                    _button("Edit", "dot_edit", approval_id, None),
                    _button("Reject", "dot_reject", approval_id, "danger"),
                ],
            }
        )
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]})
    return blocks


def _button(text: str, action_id: str, approval_id: str, style: str | None) -> Json:
    button: Json = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": approval_id,
    }
    if style is not None:
        button["style"] = style
    return button


def outcome_text(change: CardChange) -> str:
    label = _DECIDED.get(change.status, escape(change.status))
    if change.decided_by is None:
        return label
    who = f"<@{change.decided_by}>" if _SLACK_USER.fullmatch(change.decided_by) else escape(change.decided_by)
    return f"{label} by {who}"


class SlackInboundRouter:
    """Turns verified Slack events into inbox rows for the right dot, or a refusal."""

    def __init__(self, repos: Repositories, outbox: Outbox) -> None:
        self._repos = repos
        self._outbox = outbox

    def route(self, event: Json, *, bot_user_id: str | None) -> tuple[Inbound | None, str | None]:
        """The accepted message, or a notice for the sender. Both None means ignore."""
        if event.get("bot_id") or event.get("subtype") or not event.get("user") or not event.get("ts"):
            return None, None
        if event.get("type") == "message" and event.get("channel_type") != "im":
            return None, None
        channel, user = str(event["channel"]), str(event["user"])
        if not self._outbox.first_seen(CHANNEL, f"{channel}:{event['ts']}"):
            return None, None
        dot = self._dot_for(event, channel, user)
        if isinstance(dot, str):
            return None, dot
        text = str(event.get("text", ""))
        text = text.replace(f"<@{bot_user_id}>", "") if bot_user_id else _MENTION.sub("", text, count=1)
        if not text.strip():
            return None, None
        thread_ts = str(event.get("thread_ts") or event["ts"])
        return Inbound("slack", user, dot.dot_id, text.strip(), {"channel": channel, "thread_ts": thread_ts}), None

    def _dot_for(self, event: Json, channel: str, user: str) -> Dot | str:
        if event.get("type") == "app_mention":
            try:
                dot = self._repos.get_dot(self._repos.find_channel(CHANNEL, channel).dot_id)
            except NotFound:
                return NOT_LINKED
            owner = self._repos.get_user(dot.owner_user_id)
            return dot if owner.slack_user_id == user else NOT_OWNER
        try:
            owner = self._repos.find_user_by_slack(user)
        except NotFound:
            return NOT_LINKED
        dots = self._repos.list_dots_for_owner(owner.user_id)
        return dots[0] if len(dots) == 1 else NOT_LINKED


def build_app(
    repos: Repositories,
    outbox: Outbox,
    *,
    client: WebClient,
    signing_secret: str | None = None,
    redactor: Redactor | None = None,
    slack_signatures_checked: bool = True,
    process_before_response: bool = False,
    authorize: Callable[..., Any] | None = None,
) -> App:
    """The Bolt app.

    ``slack_signatures_checked=False`` skips Slack's request-signature and token
    checks. It exists only for tests that dispatch requests to the app directly;
    ``serve`` never sets it.
    """
    app = App(
        client=client,
        authorize=authorize,
        signing_secret=signing_secret,
        token_verification_enabled=slack_signatures_checked,
        request_verification_enabled=slack_signatures_checked,
        url_verification_enabled=slack_signatures_checked,
        # True runs handlers before acknowledging: for serverless HTTP and for tests.
        process_before_response=process_before_response,
    )
    router = SlackInboundRouter(repos, outbox)

    def inbound(event: Json, context: Any) -> None:
        accepted, notice = router.route(event, bot_user_id=context.get("bot_user_id"))
        if accepted is not None:
            accept(repos, accepted)
        elif notice is not None:
            _notify(client, event, notice)

    @app.event("message")
    def on_message(event: Json, context: Any) -> None:
        inbound(event, context)

    @app.event("app_mention")
    def on_mention(event: Json, context: Any) -> None:
        inbound(event, context)

    def decision(body: Json, review: ReviewDecision) -> None:
        approval_id = str(body["actions"][0]["value"])
        user = str(body["user"]["id"])
        try:
            decide(repos, approval_id, user, review, redactor=redactor)
        except PermissionError:
            _ephemeral(client, body, "You are not an approver for this dot.")
        except (ApprovalConflict, NotFound):
            _ephemeral(client, body, "This request was already decided or is no longer waiting.")

    @app.action("dot_approve")
    def on_approve(ack: Ack, body: Json) -> None:
        ack()
        decision(body, ReviewDecision(type="approve"))

    @app.action("dot_reject")
    def on_reject(ack: Ack, body: Json) -> None:
        ack()
        decision(body, ReviewDecision(type="reject", message="Rejected in Slack."))

    @app.action("dot_edit")
    def on_edit(ack: Ack, body: Json) -> None:
        ack()
        approval_id = str(body["actions"][0]["value"])
        try:
            card = repos.get_approval(approval_id)
        except NotFound:
            return
        current = json.dumps(card.args, indent=2, sort_keys=True, default=str)
        if card.status != "pending" or len(current) > SECTION_CHARS:
            _ephemeral(client, body, "This request can't be edited here. Use the web UI.")
            return
        # trigger_id is valid for three seconds: open the modal before anything slow.
        client.views_open(trigger_id=body["trigger_id"], view=_edit_view(approval_id, card.tool, current))

    @app.view("dot_edit")
    def on_edit_submit(ack: Ack, body: Json, view: Json) -> None:
        approval_id = str(view["private_metadata"])
        raw = view["state"]["values"]["args"]["json"]["value"] or ""
        try:
            edited = json.loads(raw)
        except json.JSONDecodeError:
            ack(response_action="errors", errors={"args": "Enter the arguments as a JSON object."})
            return
        if not isinstance(edited, dict):
            ack(response_action="errors", errors={"args": "Enter the arguments as a JSON object."})
            return
        try:
            decide(repos, approval_id, str(body["user"]["id"]), ReviewDecision(type="edit", edited_args=edited))
        except PermissionError:
            ack(response_action="errors", errors={"args": "You are not an approver for this dot."})
            return
        except (ApprovalConflict, NotFound):
            ack(response_action="errors", errors={"args": "This request was already decided."})
            return
        ack()

    return app


def _edit_view(approval_id: str, tool: str, current: str) -> Json:
    return {
        "type": "modal",
        "callback_id": "dot_edit",
        "private_metadata": approval_id,
        "title": {"type": "plain_text", "text": "Edit and approve"},
        "submit": {"type": "plain_text", "text": "Approve"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "args",
                "label": {"type": "plain_text", "text": f"Arguments for {tool[:60]}"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "json",
                    "multiline": True,
                    "initial_value": current,
                    "max_length": 3000,
                },
            }
        ],
    }


def _notify(client: WebClient, event: Json, text: str) -> None:
    if event.get("type") == "app_mention":
        client.chat_postEphemeral(channel=event["channel"], user=event["user"], text=text)
    else:
        client.chat_postMessage(channel=event["channel"], thread_ts=event.get("thread_ts") or event["ts"], text=text)


def _ephemeral(client: WebClient, body: Json, text: str) -> None:
    channel = (body.get("channel") or {}).get("id")
    if channel:
        client.chat_postEphemeral(channel=channel, user=body["user"]["id"], text=text)


class SlackDelivery:
    """Posts the outbox's Slack rows in order and keeps posted cards current."""

    def __init__(self, outbox: Outbox, client: WebClient) -> None:
        self._outbox = outbox
        self._client = client

    def deliver_once(self) -> bool:
        """Post the next item. False when there is nothing to post or the head item failed."""
        item = self._outbox.next(CHANNEL)
        if item is None:
            return False
        try:
            self._post(item)
        except SlackApiError as exc:
            # Never echo payloads; Slack's error code is enough to diagnose.
            error = _slack_error(exc)
            # Permanent errors park at once so they don't hold back other dots' replies.
            if self._outbox.failed(item.id, error, park=error in PERMANENT_ERRORS):
                log.warning("parked Slack outbox item %s: %s", item.id, error)
            return False
        except Exception as exc:
            self._outbox.failed(item.id, type(exc).__name__)
            return False
        self._outbox.delivered(item.id)
        return True

    def refresh_cards(self) -> int:
        """Show decisions made anywhere (Slack, web, cancel) on posted cards."""
        updated = 0
        for change in self._outbox.card_changes(CHANNEL):
            card = change.ref
            try:
                self._client.chat_update(
                    channel=card["channel"],
                    ts=card["ts"],
                    text=f"Approval {change.status}",
                    blocks=card_blocks(change.approval_id, str(card["tool"]), card["args"], outcome_text(change)),
                )
            except SlackApiError as exc:
                error = _slack_error(exc)
                if error not in PERMANENT_ERRORS:
                    continue  # retried on the next pass
                # The message or channel is gone; nothing left to show it on.
                log.warning("gave up updating Slack card %s: %s", change.approval_id, error)
            self._outbox.card_shown(change.approval_id, CHANNEL, change.status)
            updated += 1
        return updated

    def _post(self, item: OutboxItem) -> None:
        target = {"channel": item.target["channel"], "thread_ts": item.target["thread_ts"]}
        if item.kind == "message":
            # Split first: escaping first could cut an entity such as &amp; in half.
            for part in chunks(str(item.body.get("text", ""))):
                self._client.chat_postMessage(**target, text=escape(part), unfurl_links=False, unfurl_media=False)
            return
        if item.kind == "approval":
            approval_id, tool, args = str(item.body["approval_id"]), str(item.body["tool"]), item.body.get("args")
            posted = self._client.chat_postMessage(
                **target,
                text=f"Approval needed: {escape(tool)}",
                blocks=card_blocks(approval_id, tool, args),
                unfurl_links=False,
                unfurl_media=False,
            )
            ref = {"channel": posted["channel"], "ts": posted["ts"], "tool": tool, "args": args}
            self._outbox.record_card(approval_id, CHANNEL, ref)
            return
        raise ValueError(f"unknown outbox kind {item.kind!r}")


def run_delivery(delivery: SlackDelivery, stop: threading.Event, idle_s: float = 0.5) -> None:
    failures = 0
    while not stop.is_set():
        try:
            posted = delivery.deliver_once()
            delivery.refresh_cards()
        except Exception:
            # A database or network fault backs off; it must not end delivery.
            log.exception("Slack delivery pass failed")
            posted = False
        if posted:
            failures = 0
            continue
        failures = min(failures + 1, 10)
        stop.wait(idle_s * failures)


def _slack_error(exc: SlackApiError) -> str:
    return str(exc.response.get("error", "slack_api_error"))


def web_client(token: str) -> WebClient:
    client = WebClient(token=token)
    # Honour 429 Retry-After instead of failing the outbox row.
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return client


def serve() -> None:
    """Socket Mode locally: receive events and deliver the outbox until SIGINT or SIGTERM."""
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from dot.assembly import settings_redactor
    from dot.channels.outbox import PostgresOutbox
    from dot.config import get_settings
    from dot.persistence.db import PostgresRepositories, make_pool, migrate
    from dot.runtime.locks import DotLocks

    settings = get_settings()
    if not settings.database_url or not settings.slack_bot_token:
        raise SystemExit("DOT_DATABASE_URL and DOT_SLACK_BOT_TOKEN are required")
    pool = make_pool(settings.database_url)
    try:
        migrate(pool)
        repos, outbox = PostgresRepositories(pool), PostgresOutbox(pool)
        client = web_client(settings.slack_bot_token)
        app = build_app(
            repos,
            outbox,
            client=client,
            signing_secret=settings.slack_signing_secret,
            redactor=settings_redactor(settings),
        )
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        handler = None
        if settings.slack_mode == "socket":
            if not settings.slack_app_token:
                raise SystemExit("DOT_SLACK_APP_TOKEN is required for Socket Mode")
            handler = SocketModeHandler(app, settings.slack_app_token)
            handler.connect()  # type: ignore[no-untyped-call]
        # One delivery loop per deployment keeps each thread's posts in order.
        locks = DotLocks(pool)
        holding = False
        try:
            while not stop.is_set() and not (holding := locks.try_acquire(DELIVERY_LOCK)):
                stop.wait(5)
            if holding:
                run_delivery(SlackDelivery(outbox, client), stop)
        finally:
            if holding:
                locks.release(DELIVERY_LOCK)
            if handler is not None:
                handler.close()  # type: ignore[no-untyped-call]
    finally:
        pool.close()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
