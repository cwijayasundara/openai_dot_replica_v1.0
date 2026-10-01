"""Append-only, redacted safety events correlated by thread and turn."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dot.middleware.redaction import Redactor
from dot.persistence.db import AuditEvent, Repositories

if TYPE_CHECKING:
    from dot.safety.policy import PolicyResolver


class AuditWriter:
    def __init__(
        self,
        repos: Repositories | None,
        dot_id: str,
        thread_id: str,
        profile: str,
        actor: str,
        redactor: Redactor,
        policy: PolicyResolver,
    ) -> None:
        self.repos, self.dot_id, self.thread_id = repos, dot_id, thread_id
        self.profile, self.actor, self.redactor, self.policy = profile, actor, redactor, policy

    def event(
        self,
        kind: str,
        tool: str | None = None,
        *,
        state: Any = None,
        decision: str | None = None,
        verdict: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        effect = dict(self.policy.effects).get(tool or "")
        context = {
            "thread_id": self.thread_id,
            "profile": self.profile,
            "turn_id": (state or {}).get("audit_turn_id", ""),
        }
        return AuditEvent(
            0,
            self.dot_id,
            datetime.now(UTC),
            self.actor,
            kind,
            tool,
            effect.value if effect is not None else None,
            decision,
            self.redactor.content(verdict),
            self.redactor.content({**context, **(detail or {})}),
        )

    def record(self, kind: str, tool: str | None = None, **kwargs: Any) -> None:
        if self.repos is not None:
            self.repos.append_audit(self.event(kind, tool, **kwargs))


def approval_event(
    dot_id: str,
    thread_id: str,
    profile: str,
    turn_id: str,
    actor: str,
    approval_id: str,
    tool: str,
    args: dict[str, Any],
    decision: str,
    redactor: Redactor,
    edit: dict[str, Any] | None = None,
) -> AuditEvent:
    return AuditEvent(
        0,
        dot_id,
        datetime.now(UTC),
        actor,
        "approval",
        tool,
        decision=decision,
        detail=redactor.content(
            {
                "thread_id": thread_id,
                "profile": profile,
                "turn_id": turn_id,
                "approval_id": approval_id,
                "args": args,
                "edit": edit,
            }
        ),
    )
