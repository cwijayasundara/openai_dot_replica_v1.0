"""CLI: ``dot create``, ``dot run``, ``dot tail``.

``dot run`` executes one supervisor turn in this process so a demo works without
the worker. The API does not do that. ``dot tail`` listens for ``dot_events``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
from langchain_core.language_models import BaseChatModel

from dot.assembly import GraphRuntime, build_graph_runtime
from dot.config import Settings, get_settings
from dot.packs.loader import PackLoadError
from dot.persistence.db import ChannelBinding, InboxMessage, NotFound, Repositories, open_repositories
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.surfaces.dots import create_dot, last_assistant_text, read_thread


def execute(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    model: BaseChatModel | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="dot")
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="create a dot from a pack")
    create_parser.add_argument("pack")
    create_parser.add_argument("--owner", default="local")

    run_parser = sub.add_parser("run", help="create a dot and run one message")
    run_parser.add_argument("pack")
    run_parser.add_argument("message")
    run_parser.add_argument("--owner", default="local")

    tail_parser = sub.add_parser("tail", help="print live events for a dot")
    tail_parser.add_argument("dot_id")

    link_parser = sub.add_parser("link-slack", help="link the dot owner's Slack account, and optionally a channel")
    link_parser.add_argument("dot_id")
    link_parser.add_argument("--user", required=True, help="the owner's Slack user id, e.g. U123ABC")
    link_parser.add_argument("--channel", help="a Slack channel id where mentions reach this dot")

    args = parser.parse_args(argv)
    settings = settings or get_settings()
    try:
        if args.command == "create":
            dot = _with_runtime(settings, lambda repos, runtime: create_dot(repos, runtime, args.pack, args.owner))
            print(dot.dot_id)
            return 0
        if args.command == "run":
            text = _run(settings, args.pack, args.message, args.owner, model)
            if not text:
                print("the dot returned no answer", file=sys.stderr)
                return 1
            print(text)
            return 0
        if args.command == "link-slack":
            _with_runtime(settings, lambda repos, runtime: link_slack(repos, args.dot_id, args.user, args.channel))
            print(f"linked {args.user} to {args.dot_id}" + (f" and channel {args.channel}" if args.channel else ""))
            return 0
        if args.command == "tail":
            for payload in iter_tail(settings.database_url or "", args.dot_id):
                print(f"{payload['kind']} {json.dumps(payload['detail'], sort_keys=True)}", flush=True)
            return 0
    except (PackLoadError, NotFound, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"unknown command {args.command}", file=sys.stderr)
    return 2


def link_slack(repos: Repositories, dot_id: str, slack_user_id: str, channel_id: str | None = None) -> None:
    """DMs from ``slack_user_id`` reach the owner's dot; mentions in ``channel_id`` reach this dot."""
    if not re.fullmatch(r"[UW][A-Z0-9]{2,}", slack_user_id):
        raise ValueError("expected a Slack user id such as U123ABC")
    if channel_id is not None and not re.fullmatch(r"[CG][A-Z0-9]{2,}", channel_id):
        raise ValueError("expected a Slack channel id such as C123ABC")
    dot = repos.get_dot(dot_id)
    owner = repos.get_user(dot.owner_user_id)
    with contextlib.suppress(NotFound):
        if repos.find_user_by_slack(slack_user_id).user_id != owner.user_id:
            raise ValueError(f"Slack user {slack_user_id} is already linked to another user")
    if channel_id is not None:
        with contextlib.suppress(NotFound):
            if repos.find_channel("slack", channel_id).dot_id != dot_id:
                raise ValueError(f"Slack channel {channel_id} is already linked to another dot")
    repos.update_user(replace(owner, slack_user_id=slack_user_id))
    if channel_id is None:
        return
    binding = ChannelBinding(dot_id, "slack", channel_id)
    try:
        repos.get_channel(dot_id, "slack")
    except NotFound:
        repos.bind_channel(binding)
    else:
        repos.update_channel(binding)


def iter_tail(
    database_url: str,
    dot_id: str,
    *,
    timeout_s: float | None = None,
    on_listen: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield ``dot_events`` payloads for one dot. No timeout listens until the process stops."""
    if not database_url:
        raise ValueError("DOT_DATABASE_URL is required for dot tail")
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("LISTEN dot_events")
        if on_listen is not None:
            on_listen()
        for notice in conn.notifies(timeout=timeout_s):
            if notice.channel != "dot_events":
                continue
            payload = json.loads(notice.payload)
            if isinstance(payload, dict) and payload.get("dot_id") == dot_id:
                yield payload


def _run(settings: Settings, pack: str, message: str, owner: str, model: BaseChatModel | None) -> str:
    def run(repos: Repositories, runtime: GraphRuntime) -> str:
        dot = create_dot(repos, runtime, pack, owner)
        batch = [InboxMessage(0, dot.dot_id, "web", {"text": message}, "chat", datetime.now(UTC))]
        run_agent_turn(dot, "chat", batch, InMemoryEventChannel(), settings=settings, runtime=runtime, model=model)
        return last_assistant_text(read_thread(dot, settings, runtime, model))

    return _with_runtime(settings, run)


def _with_runtime[T](settings: Settings, use: Callable[[Repositories, GraphRuntime], T]) -> T:
    repos = open_repositories(settings.database_url)
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    try:
        return use(repos, runtime)
    finally:
        runtime.close()
        repos.close()


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(execute(argv))


if __name__ == "__main__":
    main()
