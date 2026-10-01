"""Persist HITL cards and queue authorized decisions; surfaces never execute."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, model_validator

from dot.config import get_settings
from dot.middleware.redaction import Redactor
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import (
    Approval,
    ApprovalConflict,
    AuditEvent,
    Dot,
    Episode,
    InboxMessage,
    Job,
    Json,
    Repositories,
)
from dot.runtime.router import latest_channel
from dot.runtime.turns import EventChannel, TurnEvent
from dot.safety.audit import approval_event


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["approve", "edit", "reject"]
    edited_args: dict[str, Any] | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> ReviewDecision:
        if (self.type == "edit") != (self.edited_args is not None):
            raise ValueError("edited_args is required only for edit")
        if self.message is not None and self.type != "reject":
            raise ValueError("message is allowed only for reject")
        return self


def persist_interrupts(
    repos: Repositories,
    dot: Dot,
    profile: str,
    snapshot: Any,
    events: EventChannel,
    *,
    redactor: Redactor | None = None,
) -> None:
    if not snapshot.interrupts:
        repos.update_dot(replace(repos.get_dot(dot.dot_id), status="active"))
        return
    _, cards, audit = _review(dot.dot_id, dot.thread_id, profile, snapshot, redactor, {})
    added = repos.pause_for_approvals(replace(repos.get_dot(dot.dot_id), status="paused"), cards, audit)
    # The card goes where the request that paused the thread came from.
    channel = latest_channel(snapshot.values.get("messages", []))
    _announce(dot.dot_id, added, events, {"channel": channel} if channel is not None else {})


def persist_job_interrupts(
    repos: Repositories,
    job: Job,
    snapshot: Any,
    events: EventChannel,
    *,
    redactor: Redactor | None = None,
) -> bool:
    """Pause a running job on its review. The dot is untouched and keeps answering.

    False when the job was no longer running (for example, cancelled).
    """
    extra: Json = {"job_id": job.job_id}
    run_ref, cards, audit = _review(job.dot_id, job.thread_id, job.profile, snapshot, redactor, extra)
    added = repos.pause_job_for_approvals(job.job_id, run_ref, cards, audit)
    if added is None:
        return False
    channel = job.origin.get("channel")
    _announce(job.dot_id, added, events, {**extra, **({"channel": channel} if isinstance(channel, dict) else {})})
    return True


def _review(
    dot_id: str, thread_id: str, profile: str, snapshot: Any, redactor: Redactor | None, extra: Json
) -> tuple[str, list[Approval], dict[str, AuditEvent]]:
    """Cards for each pending action, keyed by thread and interrupt so a replay adds none."""
    manifest = []
    proposals = []
    for interrupt in snapshot.interrupts:
        ids = []
        for index, action in enumerate(interrupt.value["action_requests"]):
            card_id = str(uuid5(NAMESPACE_URL, f"{thread_id}/{interrupt.id}/{index}"))
            ids.append(card_id)
            proposals.append((card_id, action))
        manifest.append({"interrupt_id": interrupt.id, "approval_ids": ids})
    run_ref = json.dumps(
        {
            "thread_id": thread_id,
            "checkpoint_id": snapshot.config["configurable"]["checkpoint_id"],
            "profile": profile,
            "interrupts": manifest,
            "task": snapshot.values.get("guardian_instruction", ""),
            "turn_id": snapshot.values.get("audit_turn_id", ""),
            **extra,
        },
        sort_keys=True,
    )
    cards = [
        Approval(card_id, dot_id, run_ref, action["name"], action["args"], "pending") for card_id, action in proposals
    ]
    redactor = redactor if redactor is not None else Redactor()
    audit = {
        card.approval_id: approval_event(
            dot_id,
            thread_id,
            profile,
            snapshot.values.get("audit_turn_id", ""),
            "worker",
            card.approval_id,
            card.tool,
            card.args,
            "pending",
            redactor,
        )
        for card in cards
    }
    return run_ref, cards, audit


def _announce(dot_id: str, added: list[Approval], events: EventChannel, extra: Json) -> None:
    for card in added:
        events.publish(
            TurnEvent(
                dot_id,
                "approval",
                {
                    "approval_id": card.approval_id,
                    "tool": card.tool,
                    "args": card.args,
                    "status": "pending",
                    "allowed_decisions": ["approve", "edit", "reject"],
                    **extra,
                },
            )
        )


def approvers(pack_name: str) -> frozenset[str]:
    """The pack's approvers plus this deployment's additions (``DOT_PACK_APPROVERS``).

    Deployment ids (for example a Slack test user) never go in the shipped pack.
    """
    extra = get_settings().pack_approvers.get(pack_name, [])
    return frozenset(load_pack(REPO_ROOT / "packs" / pack_name).policy.approvers) | frozenset(extra)


def decide(
    repos: Repositories, approval_id: str, user_id: str, decision: ReviewDecision, *, redactor: Redactor | None = None
) -> Approval:
    card = repos.get_approval(approval_id)
    dot = repos.get_dot(card.dot_id)
    if user_id not in approvers(dot.pack_name):
        raise PermissionError("user is not a pack approver")
    reference = json.loads(card.run_ref)
    job_id = reference.get("job_id")
    now = datetime.now(UTC)
    decided = replace(
        card, status=decision.type, decided_by=user_id, decided_at=now, edit=decision.model_dump(exclude_none=True)
    )
    episode = Episode(
        0,
        dot.dot_id,
        now,
        reference["task"],
        {"approval_id": approval_id, "tool": card.tool, "args": card.args},
        decision.type,
        {"decided_by": user_id, "decision": decided.edit, "execution": "not_yet_resumed"},
    )
    # A job resumes when a runner claims it again; a dot through its inbox.
    resume = (
        None
        if job_id is not None
        else InboxMessage(0, dot.dot_id, "approval", {"run_ref": card.run_ref}, reference["profile"], now)
    )
    redactor = redactor if redactor is not None else Redactor()
    audit = approval_event(
        dot.dot_id,
        reference["thread_id"],
        reference["profile"],
        reference.get("turn_id", ""),
        user_id,
        approval_id,
        card.tool,
        card.args,
        decision.type,
        redactor,
        decided.edit,
    )
    repos.decide_approval(decided, episode, resume, audit, job_id=job_id)
    return decided


def resume_command(repos: Repositories, dot: Dot, message: InboxMessage, snapshot: Any) -> Command[Any]:
    run_ref = str(message.payload["run_ref"])
    reference = json.loads(run_ref)
    if reference["thread_id"] != dot.thread_id or reference["profile"] != message.profile:
        raise ApprovalConflict("stale approval resume")
    return _command(repos, dot.dot_id, run_ref, snapshot)


def resume_job_command(repos: Repositories, job: Job, snapshot: Any) -> Command[Any]:
    """The decided review a paused job resumes with, checked against its current checkpoint."""
    if job.pending_run_ref is None:
        raise ApprovalConflict("job has no pending review")
    reference = json.loads(job.pending_run_ref)
    if reference["thread_id"] != job.thread_id or reference.get("job_id") != job.job_id:
        raise ApprovalConflict("stale approval resume")
    return _command(repos, job.dot_id, job.pending_run_ref, snapshot)


def _command(repos: Repositories, dot_id: str, run_ref: str, snapshot: Any) -> Command[Any]:
    reference = json.loads(run_ref)
    if reference["checkpoint_id"] != snapshot.config["configurable"]["checkpoint_id"] or {
        i.id for i in snapshot.interrupts
    } != {i["interrupt_id"] for i in reference["interrupts"]}:
        raise ApprovalConflict("stale approval resume")
    cards = {a.approval_id: a for a in repos.list_approvals(dot_id, run_ref)}
    responses = {}
    for interrupt in reference["interrupts"]:
        decisions = []
        for card_id in interrupt["approval_ids"]:
            card = cards[card_id]
            if card.status not in {"approve", "edit", "reject"}:
                raise ApprovalConflict("review is incomplete")
            response: dict[str, Any] = {"type": card.status}
            if card.status == "edit":
                assert card.edit is not None
                response["edited_action"] = {"name": card.tool, "args": card.edit["edited_args"]}
            elif card.status == "reject" and card.edit is not None:
                response["message"] = card.edit.get("message") or "Rejected by human reviewer."
            decisions.append(response)
        responses[interrupt["interrupt_id"]] = {"decisions": decisions}
    return Command(resume=responses)
