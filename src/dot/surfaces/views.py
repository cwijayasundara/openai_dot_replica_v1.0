"""Compact JSON for the web UI's read routes. Everything stored as free text is redacted."""

from __future__ import annotations

import json
from datetime import datetime

from dot.middleware.redaction import Redactor
from dot.persistence.db import Approval, AuditEvent, Finding, Job, Json, MemoryVersion

_INSTRUCTIONS_CHARS = 500


def _at(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def job_view(job: Job, redactor: Redactor) -> Json:
    instructions = job.instructions
    if len(instructions) > _INSTRUCTIONS_CHARS:
        instructions = instructions[:_INSTRUCTIONS_CHARS] + "…"
    return {
        "job_id": job.job_id,
        "subagent": job.subagent,
        "status": job.status,
        "profile": job.profile,
        "instructions": redactor.text(instructions),
        "updates": len(job.updates),
        "result_ref": job.result_ref,
        "error": redactor.text(job.error) if job.error is not None else None,
        "created_at": _at(job.created_at),
        "started_at": _at(job.started_at),
        "finished_at": _at(job.finished_at),
    }


def approval_view(card: Approval, redactor: Redactor) -> Json:
    reference = json.loads(card.run_ref)
    edit = redactor.content(card.edit) if card.edit is not None else None
    return {
        "approval_id": card.approval_id,
        "tool": card.tool,
        # The arguments the agent proposed; an edit's arguments are in ``edit.edited_args``.
        "args": redactor.content(card.args),
        "status": card.status,
        "job_id": reference.get("job_id"),
        "decided_by": card.decided_by,
        "decided_at": _at(card.decided_at),
        "edit": edit,
        "allowed_decisions": ["approve", "edit", "reject"] if card.status == "pending" else [],
    }


def audit_view(event: AuditEvent, redactor: Redactor) -> Json:
    return {
        "id": event.id,
        "dot_id": event.dot_id,
        "at": _at(event.at),
        "actor": event.actor,
        "kind": event.kind,
        "tool": event.tool,
        "effect": event.effect,
        "decision": event.decision,
        "verdict": redactor.content(event.verdict),
        "detail": redactor.content(event.detail),
    }


def finding_view(finding: Finding, redactor: Redactor) -> Json:
    return {
        "id": finding.id,
        "schedule": finding.schedule,
        "title": redactor.text(finding.title),
        "evidence": redactor.content(finding.evidence),
        "score": finding.score,
        "status": finding.status,
        "created_at": _at(finding.created_at),
    }


def memory_version_view(version: MemoryVersion, redactor: Redactor) -> Json:
    return {
        "id": version.id,
        "at": _at(version.at),
        "diff": redactor.text(version.diff),
        "episodes": version.episodes,
        "status": version.status,
        # "before" is the whole prior file, kept for rollback; the diff already shows the change.
        "detail": redactor.content({k: v for k, v in version.detail.items() if k != "before"}),
    }
