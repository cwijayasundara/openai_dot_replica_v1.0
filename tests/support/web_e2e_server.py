"""The API for the web UI's Playwright suite: real routes and graph, scripted model, no network.

Run with ``uv run python -m tests.support.web_e2e_server [port]``. One thread
drains the in-memory inbox and job queue in order, so the scripted story is
deterministic. ``POST /__e2e/slack`` routes a DM through the Slack adapter's
inbound router, so the thread holds a message that really came from Slack.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from dot.assembly import GraphRuntime, build_graph_runtime, dot_artifacts
from dot.channels.base import accept
from dot.channels.outbox import MemoryOutbox
from dot.channels.slack import SlackInboundRouter
from dot.config import Settings
from dot.jobs.runner import JobRunner, run_job
from dot.jobs.store import MemoryJobStore
from dot.middleware.redaction import Redactor
from dot.persistence.db import Dot, InboxMessage, MemoryRepositories
from dot.runtime.router import message_detail
from dot.runtime.turns import InMemoryEventChannel, TurnEvent
from dot.runtime.worker import run_agent_turn
from dot.safety.credentials import CredentialBroker, EnvironmentSecrets
from dot.surfaces.api import create_app
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

USER = "ada"
SLACK_USER = "U0ADA"
SLACK_DM = "D0ADA"
PACK = "research-analyst"
_IDLE_S = 0.1


# As the worker: these rows run as a turn of their own.
_ALONE = ("approval", "schedule")


class _Search:
    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        del limit
        return [Hit(title="Open dot runtimes", url="https://example.com/runtimes", snippet=query)]


class _Email:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, *, to: str, subject: str, body: str, credential: str) -> str:
        del subject, body, credential
        self.sent.append(to)
        return f"queued for {to}"


def respond(messages: list[BaseMessage]) -> AIMessage:
    """The story's model: decides from the last message, so turn order does not matter."""
    last = messages[-1]
    if isinstance(last, ToolMessage):
        replies = {
            "start_job": "Started a research job. I'll report back here.",
            "web_search": "Brief: two open dot runtimes found.",
            "send_email": "Email sent to Sam.",
        }
        return say(replies.get(str(last.name), "Done."))
    text = str(last.content) if isinstance(last, HumanMessage) else ""
    if text.startswith("[job_result]"):
        return tools(call("send_email", to="sam@example.com", subject="Open dot runtimes", body="Two runtimes found."))
    if text.startswith("[slack]"):
        return say("Got your Slack message.")
    if text.startswith("[web]") and "research" in text.lower():
        return tools(call("start_job", subagent="researcher", instructions="Find open dot runtimes."))
    if not text.startswith("["):  # a job's instructions
        return tools(call("web_search", query="open dot runtimes"))
    return say("Noted.")


class Story:
    def __init__(self, root: Path) -> None:
        self.settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=None,
            object_root=str(root),
            web_auth="dev",
            web_dev_user=USER,
        )
        self.repos = MemoryRepositories()
        self.store = MemoryJobStore(self.repos)
        self.events = InMemoryEventChannel()
        self.outbox = MemoryOutbox(self.repos)
        self.model = ScriptedChatModel(script=list(itertools.repeat(respond, 10_000)))
        self.email = _Email()
        self.runtime: GraphRuntime = build_graph_runtime(self.settings)
        self.runtime.audit_repositories = self.repos
        self.runtime.jobs = self.store
        self.jobs = JobRunner(
            self.store,
            self.repos,
            self.events,
            lambda dot, job: run_job(
                dot,
                job,
                self.store,
                settings=self.settings,
                runtime=self.runtime,
                model=self.model,
                deps=self.deps(dot),
            ),
            lambda dot_id: dot_artifacts(self.settings, dot_id),
            self.runtime.redactor,
        )
        self._slack_ts = itertools.count(1)

    def deps(self, dot: Dot) -> ToolDeps:
        broker = CredentialBroker(
            {"cred:smtp": "E2E_SMTP"}, EnvironmentSecrets({"E2E_SMTP": "e2e-credential"}), Redactor()
        )
        return ToolDeps(
            dot_artifacts(self.settings, dot.dot_id), search=_Search(), email=self.email, credentials=broker
        )

    def drain(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not (self._turn() or self.jobs.run_once()):
                stop.wait(_IDLE_S)

    def _turn(self) -> bool:
        """The worker's claim rule on the in-memory inbox: one dot, its leading same-profile run."""
        pending = sorted(
            (m for m in self.repos.inbox.values() if m.done_at is None and m.claimed_at is None),
            key=lambda m: (m.created_at, m.id),
        )
        for first in pending:
            dot = self.repos.get_dot(first.dot_id)
            if dot.status == "paused" and first.source != "approval":
                continue
            batch = [first] if first.source in _ALONE else self._run(pending, first)
            self._execute(dot, batch)
            return True
        return False

    @staticmethod
    def _run(pending: Sequence[InboxMessage], first: InboxMessage) -> list[InboxMessage]:
        batch: list[InboxMessage] = []
        for message in pending:
            if message.dot_id != first.dot_id:
                continue
            if message.source in _ALONE or message.profile != first.profile:
                break
            batch.append(message)
        return batch

    def _execute(self, dot: Dot, batch: list[InboxMessage]) -> None:
        now = datetime.now(UTC)
        for message in batch:
            self.repos.update_inbox(replace(message, claimed_at=now))
            if message.source != "approval":
                self.events.publish(TurnEvent(dot.dot_id, "message", message_detail(message)))
        error = None
        try:
            run_agent_turn(
                dot,
                batch[0].profile,
                batch,
                self.events,
                settings=self.settings,
                runtime=self.runtime,
                model=self.model,
                deps=self.deps(dot),
            )
        except Exception as exc:  # the worker records the error and moves on
            error = str(exc)
            self.events.publish(TurnEvent(dot.dot_id, "error", {"error": error}))
        done = datetime.now(UTC)
        for message in batch:
            self.repos.update_inbox(replace(self.repos.get_inbox(message.id), done_at=done, error=error))

    def slack_dm(self, text: str) -> InboxMessage:
        owner = self.repos.get_user(USER)
        if owner.slack_user_id is None:
            self.repos.update_user(replace(owner, slack_user_id=SLACK_USER))
        ts = f"{next(self._slack_ts)}.0001"
        event = {"type": "message", "channel_type": "im", "channel": SLACK_DM, "user": SLACK_USER, "ts": ts}
        accepted, notice = SlackInboundRouter(self.repos, self.outbox).route({**event, "text": text}, bot_user_id=None)
        if accepted is None:
            raise HTTPException(409, notice or "Slack message was not accepted")
        return accept(self.repos, accepted)


class SlackBody(BaseModel):
    text: str


def build(story: Story) -> FastAPI:
    app = create_app(story.settings, repos=story.repos, runtime=story.runtime, events=story.events, model=story.model)

    @app.post("/__e2e/slack", status_code=202)
    def slack(body: SlackBody) -> dict[str, int]:
        return {"inbox_id": story.slack_dm(body.text).id}

    @app.get("/__e2e/sent")
    def sent() -> dict[str, list[str]]:
        return {"to": story.email.sent}

    return app


def main(argv: list[str]) -> None:
    port = int(argv[0]) if argv else int(os.environ.get("E2E_API_PORT", "8765"))
    # The approver check reads process settings, not the Settings built here.
    os.environ["DOT_PACK_APPROVERS"] = json.dumps({PACK: [USER]})
    with tempfile.TemporaryDirectory(prefix="dot-e2e-") as root:
        story = Story(Path(root))
        stop = threading.Event()
        worker = threading.Thread(target=story.drain, args=(stop,), name="e2e-worker", daemon=True)
        worker.start()
        try:
            uvicorn.run(build(story), host="127.0.0.1", port=port, log_level="warning")
        finally:
            stop.set()
            worker.join(timeout=5)


if __name__ == "__main__":
    main(sys.argv[1:])
