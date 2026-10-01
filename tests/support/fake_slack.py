"""A WebClient that records calls instead of reaching Slack."""

from __future__ import annotations

import itertools
import threading
from typing import Any

from slack_bolt.authorization import AuthorizeResult
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

BOT_USER = "UBOT"


class FakeSlack(WebClient):
    def __init__(self) -> None:
        super().__init__(token="xoxb-test")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Errors to raise, in order, per API method.
        self.failures: dict[str, list[str]] = {}
        self._ts = itertools.count(1)
        self._lock = threading.Lock()

    def api_call(  # type: ignore[override]
        self,
        api_method: str,
        *,
        http_verb: str = "POST",
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
    ) -> SlackResponse:
        del files, headers, auth
        args = {**(params or {}), **(data or {}), **(json or {})}
        with self._lock:
            if api_method == "auth.test":
                body: dict[str, Any] = {"ok": True, "user_id": BOT_USER, "bot_id": "B1", "team_id": "T1"}
                return self._response(api_method, http_verb, body)
            self.calls.append((api_method, args))
            if self.failures.get(api_method):
                error = self.failures[api_method].pop(0)
                raise SlackApiError(error, self._response(api_method, http_verb, {"ok": False, "error": error}))
            body = {"ok": True, "channel": args.get("channel"), "ts": f"{next(self._ts)}.000100"}
            return self._response(api_method, http_verb, body)

    def _response(self, method: str, verb: str, body: dict[str, Any]) -> SlackResponse:
        return SlackResponse(
            client=self,
            http_verb=verb,
            api_url=f"https://slack.test/api/{method}",
            req_args={},
            data=body,
            headers={},
            status_code=200,
        )

    def made(self, method: str) -> list[dict[str, Any]]:
        with self._lock:
            return [args for name, args in self.calls if name == method]


def event(event: dict[str, Any], event_id: str = "Ev1") -> dict[str, Any]:
    return {"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event": event, "event_id": event_id}


def dm(user: str, text: str, ts: str, **extra: Any) -> dict[str, Any]:
    return event(
        {"type": "message", "channel_type": "im", "channel": "D1", "user": user, "text": text, "ts": ts, **extra}
    )


def mention(user: str, channel: str, text: str, ts: str, **extra: Any) -> dict[str, Any]:
    return event({"type": "app_mention", "channel": channel, "user": user, "text": text, "ts": ts, **extra})


def click(action_id: str, approval_id: str, user: str, channel: str = "D1") -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "channel": {"id": channel},
        "trigger_id": "trigger-1",
        "actions": [{"action_id": action_id, "block_id": "dot_review", "value": approval_id, "type": "button"}],
    }


def submit_edit(approval_id: str, user: str, raw: str) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": "T1"},
        "user": {"id": user},
        "view": {
            "id": "V1",
            "type": "modal",
            "callback_id": "dot_edit",
            "private_metadata": approval_id,
            "state": {"values": {"args": {"json": {"type": "plain_text_input", "value": raw}}}},
        },
    }


def authorize(**_: Any) -> AuthorizeResult:
    """Bolt's per-request authorization, without calling Slack's auth.test."""
    return AuthorizeResult(enterprise_id=None, team_id="T1", bot_user_id=BOT_USER, bot_id="B1", bot_token="xoxb-test")
