"""How the worker runs a scheduled inbox row. Code decides each of these, never the model.

- A sweep runs on a thread of its own, so its working does not fill the
  user's thread. Its replies go nowhere.
- While an approval card waits, the dot is paused and none of its schedules
  is queued or claimed, sweeps included (see ``scheduler.trigger``).
- A digest runs on the dot's thread. It is skipped when nothing is open. Its
  request carries a snapshot of the open findings, and its reply goes to the
  dot's default channel: the bound Slack channel, else the web thread only.
  After a turn that ends in a reply, or waits at an approval, exactly that snapshot is marked reported.
- Both run under the schedule's budget. A run stopped by it leaves a finding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from dot.config import Settings
from dot.middleware.budget import RunBudget
from dot.middleware.guardian import GUARDIAN_INSTRUCTION_KEY
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Schedule
from dot.persistence.db import Dot, Finding, InboxMessage, Json, NotFound, Repositories
from dot.proactive.findings import mark_reported, open_for_digest, record_budget_stop, render_digest
from dot.runtime.router import CHANNEL_KEY
from dot.runtime.turns import EventChannel, TurnEvent


@dataclass(frozen=True)
class ScheduledRun:
    schedule: Schedule
    budget: RunBudget
    thread_id: str
    # A digest's snapshot of open findings; empty for a sweep.
    findings: tuple[Finding, ...] = ()


def schedule_of(dot: Dot, message: InboxMessage) -> Schedule:
    """The pack schedule a schedule row was queued for."""
    pack = load_pack(REPO_ROOT / "packs" / dot.pack_name).pack
    name = message.payload.get("schedule")
    try:
        schedule = pack.schedule(name if isinstance(name, str) else "")
    except KeyError:
        raise ValueError(f"pack {pack.name!r} has no schedule {name!r}") from None
    if schedule.inbox_profile != message.profile:
        raise ValueError(f"schedule {schedule.name!r} runs profile {schedule.inbox_profile!r}, not {message.profile!r}")
    return schedule


def prepare(repos: Repositories, dot: Dot, message: InboxMessage, settings: Settings) -> ScheduledRun | None:
    """The run for one schedule row, or None when a digest has nothing to report."""
    schedule = schedule_of(dot, message)
    if schedule.kind == "reflection":
        raise ValueError("a reflection runs no agent")
    budget = RunBudget(
        schedule.max_model_calls or settings.schedule_max_model_calls,
        schedule.max_tokens or settings.schedule_max_tokens,
    )
    if schedule.kind == "digest":
        findings = open_for_digest(repos, dot.dot_id, schedule.findings_from, own=schedule.name)
        if not findings:
            return None
        return ScheduledRun(schedule, budget, dot.thread_id, tuple(findings))
    return ScheduledRun(schedule, budget, f"{dot.thread_id}:{schedule.name}:{message.id}")


def request(repos: Repositories, dot: Dot, run: ScheduledRun, message: InboxMessage) -> HumanMessage:
    """The scheduled turn's human message. The prompt is the pack's, not the row's."""
    prompt = run.schedule.prompt
    channel: Json = {"source": "schedule", "inbox_id": message.id}
    tags: dict[str, Any] = {}
    text = prompt
    if run.schedule.kind == "digest":
        text = render_digest(prompt, run.findings)
        # The Guardian reviews against the schedule's prompt, never the findings' text.
        tags[GUARDIAN_INSTRUCTION_KEY] = prompt
        target = default_channel(repos, dot.dot_id)
        if target is not None:
            channel = {"source": "schedule", "inbox_id": message.id, "via": "slack", "reply_ref": target}
    tags[CHANNEL_KEY] = channel
    return HumanMessage(content=f"[schedule] {text}", additional_kwargs=tags)


def default_channel(repos: Repositories, dot_id: str) -> Json | None:
    """The Slack channel bound to this dot, as a reply address. No thread: the digest starts one."""
    try:
        binding = repos.get_channel(dot_id, "slack")
    except NotFound:
        return None
    return {"channel": binding.external_id}


def finish(repos: Repositories, dot: Dot, run: ScheduledRun, messages: Sequence[Any]) -> None:
    if run.budget.stopped:
        record_budget_stop(repos, dot.dot_id, run.schedule.name, run.budget.usage())
        return
    if run.schedule.kind == "digest" and (_replied(messages) or _awaiting_approval(messages)):
        # A turn paused at an approval acted on its findings; reporting them again would raise a second card.
        mark_reported(repos, run.findings)


class Silent:
    """A sweep's events without its words: tool activity shows, nothing reads as a message to the user."""

    def __init__(self, inner: EventChannel) -> None:
        self._inner = inner

    def publish(self, event: TurnEvent) -> None:
        if event.kind != "message":
            self._inner.publish(event)


def _replied(messages: Sequence[Any]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, AIMessage) and not last.tool_calls and bool(last.text.strip())


def _awaiting_approval(messages: Sequence[Any]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, AIMessage) and bool(last.tool_calls)
