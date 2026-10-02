"""Pool, migrations and typed repositories for the app tables.

``open_repositories(None)`` is the in-memory implementation. It passes the same
contract tests as Postgres. The audit log rejects updates in both.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).parent / "migrations"
Json = dict[str, Any]


def _insert_audit(conn: psycopg.Connection[Any], event: AuditEvent) -> None:
    conn.execute(
        "INSERT INTO audit_log (dot_id, at, actor, kind, tool, effect, decision, verdict, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            event.dot_id,
            event.at,
            event.actor,
            event.kind,
            event.tool,
            event.effect,
            event.decision,
            Jsonb(event.verdict) if event.verdict is not None else None,
            Jsonb(event.detail) if event.detail is not None else None,
        ),
    )


class NotFound(Exception):
    def __init__(self, table: str, key: str) -> None:
        super().__init__(f"{table} {key} not found")
        self.table = table
        self.key = key


class AppendOnly(Exception):
    """Raised when a caller tries to change the audit log."""


class ApprovalConflict(Exception):
    """A review was already decided or no longer belongs to a paused run."""


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str
    slack_user_id: str | None = None
    web_subject: str | None = None


@dataclass(frozen=True)
class Dot:
    dot_id: str
    owner_user_id: str
    pack_name: str
    pack_version: str
    thread_id: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class InboxMessage:
    id: int
    dot_id: str
    source: str
    payload: Json
    profile: str
    created_at: datetime
    claimed_at: datetime | None = None
    done_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class Job:
    job_id: str
    dot_id: str
    subagent: str
    instructions: str
    status: str
    thread_id: str
    updates: list[Json]
    created_at: datetime
    result_ref: str | None = None
    finished_at: datetime | None = None
    # Empty grants nothing: building a job agent for it fails.
    profile: str = ""
    origin: Json = field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    pending_run_ref: str | None = None


@dataclass(frozen=True)
class Approval:
    approval_id: str
    dot_id: str
    run_ref: str
    tool: str
    args: Json
    status: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    edit: Json | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: int
    dot_id: str
    at: datetime
    actor: str
    kind: str
    tool: str | None = None
    effect: str | None = None
    decision: str | None = None
    verdict: Json | None = None
    detail: Json | None = None


@dataclass(frozen=True)
class Finding:
    id: int
    dot_id: str
    schedule: str
    title: str
    evidence: Json
    score: float
    status: str
    created_at: datetime


@dataclass(frozen=True)
class Episode:
    id: int
    dot_id: str
    at: datetime
    task: str
    proposal: Json
    human_action: str
    outcome: Json


@dataclass(frozen=True)
class MemoryVersion:
    id: int
    dot_id: str
    at: datetime
    diff: str
    episodes: list[int]
    status: str
    detail: Json = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelBinding:
    dot_id: str
    channel: str
    external_id: str


class Repositories(Protocol):
    def close(self) -> None: ...

    def create_user(self, user: User) -> None: ...
    def get_user(self, user_id: str) -> User: ...
    def update_user(self, user: User) -> None: ...

    def create_dot(self, dot: Dot) -> None: ...
    def get_dot(self, dot_id: str) -> Dot: ...
    def update_dot(self, dot: Dot) -> None: ...

    def insert_inbox(self, message: InboxMessage) -> InboxMessage: ...
    def get_inbox(self, message_id: int) -> InboxMessage: ...
    def update_inbox(self, message: InboxMessage) -> None: ...

    def create_job(self, job: Job) -> None: ...
    def get_job(self, job_id: str) -> Job: ...
    def update_job(self, job: Job) -> None: ...

    def create_approval(self, approval: Approval) -> None: ...
    def get_approval(self, approval_id: str) -> Approval: ...
    def update_approval(self, approval: Approval) -> None: ...
    def list_approvals(self, dot_id: str, run_ref: str) -> list[Approval]: ...
    def list_dot_approvals(self, dot_id: str, status: str | None = None) -> list[Approval]:
        """Pending cards first, then the most recently decided, then closed undecided ones (cancelled)."""
        ...

    def pause_for_approvals(
        self, dot: Dot, approvals: list[Approval], audit: dict[str, AuditEvent] | None = None
    ) -> list[Approval]: ...
    def decide_approval(
        self,
        approval: Approval,
        episode: Episode,
        resume: InboxMessage | None,
        audit: AuditEvent | None = None,
        *,
        job_id: str | None = None,
    ) -> None:
        """Record one decision. The last one for a run resumes it.

        A dot run resumes through ``resume`` in the inbox; a job run (``job_id``)
        goes back to ``queued`` for a runner to claim.
        """
        ...

    def pause_job_for_approvals(
        self, job_id: str, run_ref: str, approvals: list[Approval], audit: dict[str, AuditEvent] | None = None
    ) -> list[Approval] | None:
        """Move a running job to ``paused`` with its cards. None when it was no longer running."""
        ...

    def append_audit(self, event: AuditEvent) -> AuditEvent: ...
    def get_audit(self, event_id: int) -> AuditEvent: ...
    def list_audit(
        self,
        dot_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
        turn_id: str | None = None,
        actors: Sequence[str] | None = None,
    ) -> list[AuditEvent]: ...
    def update_audit(self, event: AuditEvent) -> None: ...

    def insert_finding(self, finding: Finding) -> Finding: ...
    def get_finding(self, finding_id: int) -> Finding: ...
    def update_finding(self, finding: Finding) -> None: ...
    def list_findings(self, dot_id: str, status: str | None = None) -> list[Finding]:
        """Newest first."""
        ...

    def insert_episode(self, episode: Episode) -> Episode: ...
    def get_episode(self, episode_id: int) -> Episode: ...
    def update_episode(self, episode: Episode) -> None: ...
    def list_episodes(
        self, dot_id: str, *, after_id: int = 0, before: datetime | None = None, limit: int = 100
    ) -> list[Episode]:
        """Oldest first: episodes with ``id > after_id`` recorded before ``before``."""
        ...

    def insert_memory_version(self, version: MemoryVersion) -> MemoryVersion: ...
    def get_memory_version(self, version_id: int) -> MemoryVersion: ...
    def update_memory_version(self, version: MemoryVersion) -> None: ...
    def list_memory_versions(self, dot_id: str) -> list[MemoryVersion]:
        """Newest first."""
        ...

    def bind_channel(self, binding: ChannelBinding) -> None: ...
    def get_channel(self, dot_id: str, channel: str) -> ChannelBinding: ...
    def update_channel(self, binding: ChannelBinding) -> None: ...
    def find_channel(self, channel: str, external_id: str) -> ChannelBinding:
        """The dot bound to an external conversation, such as a Slack channel id."""
        ...

    def find_user_by_slack(self, slack_user_id: str) -> User: ...
    def find_user_by_web_subject(self, web_subject: str) -> User: ...
    def list_dots_for_owner(self, owner_user_id: str) -> list[Dot]: ...
    def list_active_dots(self, pack_name: str) -> list[Dot]: ...
    def insert_schedule_run(self, message: InboxMessage) -> InboxMessage | None:
        """Queue a scheduled run unless one for that schedule is pending or its slot already ran.

        ``payload`` must hold ``schedule`` and ``slot``. None means nothing was queued.
        """
        ...


def migrate(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))


class MemoryRepositories:
    def __init__(self) -> None:
        self._approval_lock = RLock()
        self.users: dict[str, User] = {}
        self.dots: dict[str, Dot] = {}
        self.inbox: dict[int, InboxMessage] = {}
        self.jobs: dict[str, Job] = {}
        self.approvals: dict[str, Approval] = {}
        self.audit: dict[int, AuditEvent] = {}
        self.findings: dict[int, Finding] = {}
        self.episodes: dict[int, Episode] = {}
        self.memory_versions: dict[int, MemoryVersion] = {}
        self.channels: dict[tuple[str, str], ChannelBinding] = {}
        self._seq = 0

    def close(self) -> None:
        return None

    @contextmanager
    def _approval_transaction(self) -> Iterator[None]:
        with self._approval_lock:
            # Frozen records are replaced, never mutated, inside this boundary.
            tables: list[dict[Any, Any]] = [
                self.approvals,
                self.episodes,
                self.audit,
                self.inbox,
                self.dots,
                self.jobs,
            ]
            snapshots = [dict(table) for table in tables]
            try:
                yield
            except Exception:
                for table, snapshot in zip(tables, snapshots, strict=True):
                    table.clear()
                    table.update(snapshot)
                raise

    def _next(self) -> int:
        with self._approval_lock:
            self._seq += 1
            return self._seq

    def create_user(self, user: User) -> None:
        self.users[user.user_id] = user

    def get_user(self, user_id: str) -> User:
        try:
            return self.users[user_id]
        except KeyError:
            raise NotFound("users", user_id) from None

    def update_user(self, user: User) -> None:
        self.get_user(user.user_id)
        self.users[user.user_id] = user

    def create_dot(self, dot: Dot) -> None:
        self.get_user(dot.owner_user_id)
        self.dots[dot.dot_id] = dot

    def get_dot(self, dot_id: str) -> Dot:
        try:
            return self.dots[dot_id]
        except KeyError:
            raise NotFound("dots", dot_id) from None

    def update_dot(self, dot: Dot) -> None:
        self.get_dot(dot.dot_id)
        self.dots[dot.dot_id] = dot

    def insert_inbox(self, message: InboxMessage) -> InboxMessage:
        self.get_dot(message.dot_id)
        stored = InboxMessage(
            id=self._next(),
            dot_id=message.dot_id,
            source=message.source,
            payload=message.payload,
            profile=message.profile,
            created_at=message.created_at,
            claimed_at=message.claimed_at,
            done_at=message.done_at,
            error=message.error,
        )
        self.inbox[stored.id] = stored
        return stored

    def get_inbox(self, message_id: int) -> InboxMessage:
        try:
            return self.inbox[message_id]
        except KeyError:
            raise NotFound("inbox", str(message_id)) from None

    def update_inbox(self, message: InboxMessage) -> None:
        self.get_inbox(message.id)
        self.inbox[message.id] = message

    def create_job(self, job: Job) -> None:
        self.get_dot(job.dot_id)
        self.jobs[job.job_id] = job

    def get_job(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise NotFound("jobs", job_id) from None

    def update_job(self, job: Job) -> None:
        self.get_job(job.job_id)
        self.jobs[job.job_id] = job

    def create_approval(self, approval: Approval) -> None:
        self.get_dot(approval.dot_id)
        self.approvals[approval.approval_id] = approval

    def get_approval(self, approval_id: str) -> Approval:
        try:
            return self.approvals[approval_id]
        except KeyError:
            raise NotFound("approvals", approval_id) from None

    def update_approval(self, approval: Approval) -> None:
        self.get_approval(approval.approval_id)
        self.approvals[approval.approval_id] = approval

    def list_approvals(self, dot_id: str, run_ref: str) -> list[Approval]:
        return [a for a in self.approvals.values() if a.dot_id == dot_id and a.run_ref == run_ref]

    def list_dot_approvals(self, dot_id: str, status: str | None = None) -> list[Approval]:
        self.get_dot(dot_id)
        with self._approval_lock:
            rows = [a for a in self.approvals.values() if a.dot_id == dot_id and status in (None, a.status)]
        return sorted(rows, key=_approval_order)

    def pause_for_approvals(
        self, dot: Dot, approvals: list[Approval], audit: dict[str, AuditEvent] | None = None
    ) -> list[Approval]:
        with self._approval_transaction():
            self.get_dot(dot.dot_id)
            added = [a for a in approvals if a.approval_id not in self.approvals]
            for approval in added:
                self.create_approval(approval)
                if audit is not None:
                    self.append_audit(audit[approval.approval_id])
            self.update_dot(dot)
            return added

    def decide_approval(
        self,
        approval: Approval,
        episode: Episode,
        resume: InboxMessage | None,
        audit: AuditEvent | None = None,
        *,
        job_id: str | None = None,
    ) -> None:
        with self._approval_transaction():
            if self.get_approval(approval.approval_id).status != "pending":
                raise ApprovalConflict("approval is not pending")
            if job_id is not None:
                job = self.get_job(job_id)
                if job.status != "paused" or job.pending_run_ref != approval.run_ref:
                    raise ApprovalConflict("job is not paused for this review")
            elif self.get_dot(approval.dot_id).status != "paused":
                raise ApprovalConflict("approval is not pending")
            self.update_approval(approval)
            self.insert_episode(episode)
            if audit is not None:
                self.append_audit(audit)
            if all(a.status != "pending" for a in self.list_approvals(approval.dot_id, approval.run_ref)):
                if job_id is not None:
                    self.update_job(replace(self.get_job(job_id), status="queued"))
                elif resume is not None:
                    self.insert_inbox(resume)

    def pause_job_for_approvals(
        self, job_id: str, run_ref: str, approvals: list[Approval], audit: dict[str, AuditEvent] | None = None
    ) -> list[Approval] | None:
        with self._approval_transaction():
            job = self.get_job(job_id)
            if job.status != "running":
                return None
            added = [a for a in approvals if a.approval_id not in self.approvals]
            for approval in added:
                self.create_approval(approval)
                if audit is not None:
                    self.append_audit(audit[approval.approval_id])
            self.update_job(replace(job, status="paused", pending_run_ref=run_ref))
            return added

    def append_audit(self, event: AuditEvent) -> AuditEvent:
        with self._approval_lock:
            self.get_dot(event.dot_id)
            stored = AuditEvent(
                id=self._next(),
                dot_id=event.dot_id,
                at=event.at,
                actor=event.actor,
                kind=event.kind,
                tool=event.tool,
                effect=event.effect,
                decision=event.decision,
                verdict=deepcopy(event.verdict),
                detail=deepcopy(event.detail),
            )
            self.audit[stored.id] = stored
            return deepcopy(stored)

    def get_audit(self, event_id: int) -> AuditEvent:
        try:
            return deepcopy(self.audit[event_id])
        except KeyError:
            raise NotFound("audit_log", str(event_id)) from None

    def update_audit(self, event: AuditEvent) -> None:
        self.get_audit(event.id)
        raise AppendOnly("audit_log is append-only")

    def list_audit(
        self,
        dot_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
        turn_id: str | None = None,
        actors: Sequence[str] | None = None,
    ) -> list[AuditEvent]:
        self.get_dot(dot_id)
        with self._approval_lock:
            rows = tuple(self.audit.values())
        return deepcopy(
            sorted(
                (
                    a
                    for a in rows
                    if a.dot_id == dot_id
                    and a.id > after_id
                    and (turn_id is None or (a.detail or {}).get("turn_id") == turn_id)
                    and (actors is None or a.actor in actors)
                ),
                key=lambda a: a.id,
            )[:limit]
        )

    def insert_finding(self, finding: Finding) -> Finding:
        self.get_dot(finding.dot_id)
        stored = Finding(
            id=self._next(),
            dot_id=finding.dot_id,
            schedule=finding.schedule,
            title=finding.title,
            evidence=finding.evidence,
            score=finding.score,
            status=finding.status,
            created_at=finding.created_at,
        )
        self.findings[stored.id] = stored
        return stored

    def get_finding(self, finding_id: int) -> Finding:
        try:
            return self.findings[finding_id]
        except KeyError:
            raise NotFound("findings", str(finding_id)) from None

    def update_finding(self, finding: Finding) -> None:
        self.get_finding(finding.id)
        self.findings[finding.id] = finding

    def list_findings(self, dot_id: str, status: str | None = None) -> list[Finding]:
        self.get_dot(dot_id)
        rows = [f for f in self.findings.values() if f.dot_id == dot_id and status in (None, f.status)]
        return sorted(rows, key=lambda f: (f.created_at, f.id), reverse=True)

    def insert_episode(self, episode: Episode) -> Episode:
        self.get_dot(episode.dot_id)
        stored = Episode(
            id=self._next(),
            dot_id=episode.dot_id,
            at=episode.at,
            task=episode.task,
            proposal=episode.proposal,
            human_action=episode.human_action,
            outcome=episode.outcome,
        )
        self.episodes[stored.id] = stored
        return stored

    def get_episode(self, episode_id: int) -> Episode:
        try:
            return self.episodes[episode_id]
        except KeyError:
            raise NotFound("episodes", str(episode_id)) from None

    def update_episode(self, episode: Episode) -> None:
        self.get_episode(episode.id)
        self.episodes[episode.id] = episode

    def list_episodes(
        self, dot_id: str, *, after_id: int = 0, before: datetime | None = None, limit: int = 100
    ) -> list[Episode]:
        self.get_dot(dot_id)
        rows = sorted(
            (
                e
                for e in self.episodes.values()
                if e.dot_id == dot_id and e.id > after_id and (before is None or e.at < before)
            ),
            key=lambda e: e.id,
        )
        return rows[:limit]

    def insert_memory_version(self, version: MemoryVersion) -> MemoryVersion:
        self.get_dot(version.dot_id)
        stored = MemoryVersion(
            id=self._next(),
            dot_id=version.dot_id,
            at=version.at,
            diff=version.diff,
            episodes=list(version.episodes),
            status=version.status,
            detail=dict(version.detail),
        )
        self.memory_versions[stored.id] = stored
        return stored

    def get_memory_version(self, version_id: int) -> MemoryVersion:
        try:
            return self.memory_versions[version_id]
        except KeyError:
            raise NotFound("memory_versions", str(version_id)) from None

    def update_memory_version(self, version: MemoryVersion) -> None:
        self.get_memory_version(version.id)
        self.memory_versions[version.id] = version

    def list_memory_versions(self, dot_id: str) -> list[MemoryVersion]:
        self.get_dot(dot_id)
        rows = [v for v in self.memory_versions.values() if v.dot_id == dot_id]
        return sorted(rows, key=lambda v: (v.at, v.id), reverse=True)

    def bind_channel(self, binding: ChannelBinding) -> None:
        self.get_dot(binding.dot_id)
        self.channels[(binding.dot_id, binding.channel)] = binding

    def get_channel(self, dot_id: str, channel: str) -> ChannelBinding:
        try:
            return self.channels[(dot_id, channel)]
        except KeyError:
            raise NotFound("channel_bindings", f"{dot_id}/{channel}") from None

    def update_channel(self, binding: ChannelBinding) -> None:
        self.get_channel(binding.dot_id, binding.channel)
        self.channels[(binding.dot_id, binding.channel)] = binding

    def find_channel(self, channel: str, external_id: str) -> ChannelBinding:
        for binding in self.channels.values():
            if binding.channel == channel and binding.external_id == external_id:
                return binding
        raise NotFound("channel_bindings", f"{channel}/{external_id}")

    def find_user_by_slack(self, slack_user_id: str) -> User:
        for user in self.users.values():
            if user.slack_user_id == slack_user_id:
                return user
        raise NotFound("users", slack_user_id)

    def find_user_by_web_subject(self, web_subject: str) -> User:
        for user in self.users.values():
            if user.web_subject == web_subject:
                return user
        raise NotFound("users", web_subject)

    def list_dots_for_owner(self, owner_user_id: str) -> list[Dot]:
        return sorted(
            (dot for dot in self.dots.values() if dot.owner_user_id == owner_user_id),
            key=lambda dot: (dot.created_at, dot.dot_id),
        )

    def list_active_dots(self, pack_name: str) -> list[Dot]:
        return sorted(
            (dot for dot in self.dots.values() if dot.pack_name == pack_name and dot.status == "active"),
            key=lambda dot: (dot.created_at, dot.dot_id),
        )

    def insert_schedule_run(self, message: InboxMessage) -> InboxMessage | None:
        name, slot = _schedule_key(message)
        with self._approval_lock:
            for row in self.inbox.values():
                if row.dot_id != message.dot_id or row.source != "schedule" or row.payload.get("schedule") != name:
                    continue
                if row.done_at is None or row.payload.get("slot") == slot:
                    return None
            return self.insert_inbox(message)


def _schedule_key(message: InboxMessage) -> tuple[str, str]:
    name, slot = message.payload.get("schedule"), message.payload.get("slot")
    if message.source != "schedule" or not isinstance(name, str) or not isinstance(slot, str):
        raise ValueError("a schedule run needs source 'schedule' and a payload with schedule and slot")
    return name, slot


def _approval_order(card: Approval) -> tuple[bool, float, str]:
    decided = card.decided_at.timestamp() if card.decided_at is not None else 0.0
    return (card.status != "pending", -decided, card.approval_id)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raise TypeError(f"expected datetime, got {type(value).__name__}")


def _opt_dt(value: Any) -> datetime | None:
    return None if value is None else _dt(value)


def job_from_row(row: dict[str, Any]) -> Job:
    return Job(
        job_id=row["job_id"],
        dot_id=row["dot_id"],
        subagent=row["subagent"],
        instructions=row["instructions"],
        status=row["status"],
        thread_id=row["thread_id"],
        updates=list(row["updates"]),
        created_at=_dt(row["created_at"]),
        result_ref=row["result_ref"],
        finished_at=_opt_dt(row["finished_at"]),
        profile=row["profile"],
        origin=dict(row["origin"]),
        error=row["error"],
        started_at=_opt_dt(row["started_at"]),
        pending_run_ref=row["pending_run_ref"],
    )


class PostgresRepositories:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def close(self) -> None:
        self.pool.close()

    def pause_for_approvals(
        self, dot: Dot, approvals: list[Approval], audit: dict[str, AuditEvent] | None = None
    ) -> list[Approval]:
        added = []
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT dot_id FROM dots WHERE dot_id=%s FOR UPDATE", (dot.dot_id,)).fetchone()
            if row is None:
                raise NotFound("dots", dot.dot_id)
            for card in approvals:
                row = conn.execute(
                    "INSERT INTO approvals (approval_id, dot_id, run_ref, tool, args, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (approval_id) DO NOTHING RETURNING approval_id",
                    (card.approval_id, card.dot_id, card.run_ref, card.tool, Jsonb(card.args), card.status),
                ).fetchone()
                if row is not None:
                    added.append(card)
                    if audit is not None:
                        _insert_audit(conn, audit[card.approval_id])
            conn.execute("UPDATE dots SET status=%s WHERE dot_id=%s", (dot.status, dot.dot_id))
        return added

    def pause_job_for_approvals(
        self, job_id: str, run_ref: str, approvals: list[Approval], audit: dict[str, AuditEvent] | None = None
    ) -> list[Approval] | None:
        added = []
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE jobs SET status = 'paused', pending_run_ref = %s"
                " WHERE job_id = %s AND status = 'running' RETURNING job_id",
                (run_ref, job_id),
            ).fetchone()
            if row is None:
                return None
            for card in approvals:
                row = conn.execute(
                    "INSERT INTO approvals (approval_id, dot_id, run_ref, tool, args, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (approval_id) DO NOTHING RETURNING approval_id",
                    (card.approval_id, card.dot_id, card.run_ref, card.tool, Jsonb(card.args), card.status),
                ).fetchone()
                if row is not None:
                    added.append(card)
                    if audit is not None:
                        _insert_audit(conn, audit[card.approval_id])
        return added

    def list_approvals(self, dot_id: str, run_ref: str) -> list[Approval]:
        with self.pool.connection() as conn:
            ids = conn.execute(
                "SELECT approval_id FROM approvals WHERE dot_id = %s AND run_ref = %s", (dot_id, run_ref)
            ).fetchall()
        return [self.get_approval(str(row[0])) for row in ids]

    def list_dot_approvals(self, dot_id: str, status: str | None = None) -> list[Approval]:
        self.get_dot(dot_id)
        with self.pool.connection() as conn:
            ids = conn.execute(
                "SELECT approval_id FROM approvals WHERE dot_id = %s AND (%s::text IS NULL OR status = %s) "
                "ORDER BY status <> 'pending', decided_at DESC NULLS LAST, approval_id",
                (dot_id, status, status),
            ).fetchall()
        return [self.get_approval(str(row[0])) for row in ids]

    def decide_approval(
        self,
        approval: Approval,
        episode: Episode,
        resume: InboxMessage | None,
        audit: AuditEvent | None = None,
        *,
        job_id: str | None = None,
    ) -> None:
        # Serialize decisions for the whole paused run (the dot, or the job),
        # not just individual cards: only the last decision may resume it.
        with self.pool.connection() as conn, conn.transaction():
            if job_id is not None:
                job = conn.execute(
                    "SELECT status, pending_run_ref FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
                ).fetchone()
                if job is None or job[0] != "paused" or job[1] != approval.run_ref:
                    raise ApprovalConflict("job is not paused for this review")
            else:
                dot = conn.execute(
                    "SELECT status FROM dots WHERE dot_id = %s FOR UPDATE", (approval.dot_id,)
                ).fetchone()
                if dot is None or dot[0] != "paused":
                    raise ApprovalConflict("dot is not paused")
            result = conn.execute(
                "UPDATE approvals SET status=%s, decided_by=%s, decided_at=%s, edit=%s "
                "WHERE approval_id=%s AND status='pending' RETURNING approval_id",
                (
                    approval.status,
                    approval.decided_by,
                    approval.decided_at,
                    Jsonb(approval.edit) if approval.edit is not None else None,
                    approval.approval_id,
                ),
            ).fetchone()
            if result is None:
                raise ApprovalConflict("approval is not pending")
            conn.execute(
                "INSERT INTO episodes (dot_id, at, task, proposal, human_action, outcome) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    episode.dot_id,
                    episode.at,
                    episode.task,
                    Jsonb(episode.proposal),
                    episode.human_action,
                    Jsonb(episode.outcome),
                ),
            )
            pending = conn.execute(
                "SELECT 1 FROM approvals WHERE dot_id=%s AND run_ref=%s AND status='pending' LIMIT 1",
                (approval.dot_id, approval.run_ref),
            ).fetchone()
            if pending is None and job_id is not None:
                conn.execute("UPDATE jobs SET status = 'queued' WHERE job_id = %s", (job_id,))
            elif pending is None and resume is not None:
                conn.execute(
                    "INSERT INTO inbox (dot_id, source, payload, profile, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (resume.dot_id, resume.source, Jsonb(resume.payload), resume.profile, resume.created_at),
                )
            if audit is not None:
                _insert_audit(conn, audit)

    def create_user(self, user: User) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, display_name, slack_user_id, web_subject) VALUES (%s, %s, %s, %s)",
                (user.user_id, user.display_name, user.slack_user_id, user.web_subject),
            )

    def get_user(self, user_id: str) -> User:
        row = self._one("SELECT * FROM users WHERE user_id = %s", (user_id,), "users", user_id)
        return User(
            user_id=row["user_id"],
            display_name=row["display_name"],
            slack_user_id=row["slack_user_id"],
            web_subject=row["web_subject"],
        )

    def update_user(self, user: User) -> None:
        self._must(
            "UPDATE users SET display_name = %s, slack_user_id = %s, web_subject = %s WHERE user_id = %s",
            (user.display_name, user.slack_user_id, user.web_subject, user.user_id),
            "users",
            user.user_id,
        )

    def create_dot(self, dot: Dot) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO dots (dot_id, owner_user_id, pack_name, pack_version, thread_id, created_at, status)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    dot.dot_id,
                    dot.owner_user_id,
                    dot.pack_name,
                    dot.pack_version,
                    dot.thread_id,
                    dot.created_at,
                    dot.status,
                ),
            )

    def get_dot(self, dot_id: str) -> Dot:
        row = self._one("SELECT * FROM dots WHERE dot_id = %s", (dot_id,), "dots", dot_id)
        return Dot(
            dot_id=row["dot_id"],
            owner_user_id=row["owner_user_id"],
            pack_name=row["pack_name"],
            pack_version=row["pack_version"],
            thread_id=row["thread_id"],
            status=row["status"],
            created_at=_dt(row["created_at"]),
        )

    def update_dot(self, dot: Dot) -> None:
        self._must(
            "UPDATE dots SET owner_user_id = %s, pack_name = %s, pack_version = %s, thread_id = %s,"
            " created_at = %s, status = %s WHERE dot_id = %s",
            (dot.owner_user_id, dot.pack_name, dot.pack_version, dot.thread_id, dot.created_at, dot.status, dot.dot_id),
            "dots",
            dot.dot_id,
        )

    def insert_inbox(self, message: InboxMessage) -> InboxMessage:
        row = self._insert(
            "INSERT INTO inbox (dot_id, source, payload, profile, created_at, claimed_at, done_at, error)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                message.dot_id,
                message.source,
                Jsonb(message.payload),
                message.profile,
                message.created_at,
                message.claimed_at,
                message.done_at,
                message.error,
            ),
        )
        return self.get_inbox(int(row["id"]))

    def get_inbox(self, message_id: int) -> InboxMessage:
        row = self._one("SELECT * FROM inbox WHERE id = %s", (message_id,), "inbox", str(message_id))
        return InboxMessage(
            id=int(row["id"]),
            dot_id=row["dot_id"],
            source=row["source"],
            payload=row["payload"],
            profile=row["profile"],
            created_at=_dt(row["created_at"]),
            claimed_at=_opt_dt(row["claimed_at"]),
            done_at=_opt_dt(row["done_at"]),
            error=row["error"],
        )

    def update_inbox(self, message: InboxMessage) -> None:
        self._must(
            "UPDATE inbox SET source = %s, payload = %s, profile = %s, created_at = %s, claimed_at = %s,"
            " done_at = %s, error = %s WHERE id = %s",
            (
                message.source,
                Jsonb(message.payload),
                message.profile,
                message.created_at,
                message.claimed_at,
                message.done_at,
                message.error,
                message.id,
            ),
            "inbox",
            str(message.id),
        )

    def create_job(self, job: Job) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, dot_id, subagent, instructions, status, thread_id, updates,"
                " result_ref, created_at, finished_at, profile, origin, error, started_at, pending_run_ref)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    job.job_id,
                    job.dot_id,
                    job.subagent,
                    job.instructions,
                    job.status,
                    job.thread_id,
                    Jsonb(job.updates),
                    job.result_ref,
                    job.created_at,
                    job.finished_at,
                    job.profile,
                    Jsonb(job.origin),
                    job.error,
                    job.started_at,
                    job.pending_run_ref,
                ),
            )

    def get_job(self, job_id: str) -> Job:
        return job_from_row(self._one("SELECT * FROM jobs WHERE job_id = %s", (job_id,), "jobs", job_id))

    def update_job(self, job: Job) -> None:
        self._must(
            "UPDATE jobs SET subagent = %s, instructions = %s, status = %s, thread_id = %s, updates = %s,"
            " result_ref = %s, created_at = %s, finished_at = %s, profile = %s, origin = %s, error = %s,"
            " started_at = %s, pending_run_ref = %s WHERE job_id = %s",
            (
                job.subagent,
                job.instructions,
                job.status,
                job.thread_id,
                Jsonb(job.updates),
                job.result_ref,
                job.created_at,
                job.finished_at,
                job.profile,
                Jsonb(job.origin),
                job.error,
                job.started_at,
                job.pending_run_ref,
                job.job_id,
            ),
            "jobs",
            job.job_id,
        )

    def create_approval(self, approval: Approval) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO approvals (approval_id, dot_id, run_ref, tool, args, status, decided_by, decided_at, edit)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    approval.approval_id,
                    approval.dot_id,
                    approval.run_ref,
                    approval.tool,
                    Jsonb(approval.args),
                    approval.status,
                    approval.decided_by,
                    approval.decided_at,
                    Jsonb(approval.edit) if approval.edit is not None else None,
                ),
            )

    def get_approval(self, approval_id: str) -> Approval:
        row = self._one("SELECT * FROM approvals WHERE approval_id = %s", (approval_id,), "approvals", approval_id)
        return Approval(
            approval_id=row["approval_id"],
            dot_id=row["dot_id"],
            run_ref=row["run_ref"],
            tool=row["tool"],
            args=row["args"],
            status=row["status"],
            decided_by=row["decided_by"],
            decided_at=_opt_dt(row["decided_at"]),
            edit=row["edit"],
        )

    def update_approval(self, approval: Approval) -> None:
        self._must(
            "UPDATE approvals SET run_ref = %s, tool = %s, args = %s, status = %s, decided_by = %s,"
            " decided_at = %s, edit = %s WHERE approval_id = %s",
            (
                approval.run_ref,
                approval.tool,
                Jsonb(approval.args),
                approval.status,
                approval.decided_by,
                approval.decided_at,
                Jsonb(approval.edit) if approval.edit is not None else None,
                approval.approval_id,
            ),
            "approvals",
            approval.approval_id,
        )

    def append_audit(self, event: AuditEvent) -> AuditEvent:
        row = self._insert(
            "INSERT INTO audit_log (dot_id, at, actor, kind, tool, effect, decision, verdict, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                event.dot_id,
                event.at,
                event.actor,
                event.kind,
                event.tool,
                event.effect,
                event.decision,
                Jsonb(event.verdict) if event.verdict is not None else None,
                Jsonb(event.detail) if event.detail is not None else None,
            ),
        )
        return self.get_audit(int(row["id"]))

    def get_audit(self, event_id: int) -> AuditEvent:
        row = self._one("SELECT * FROM audit_log WHERE id = %s", (event_id,), "audit_log", str(event_id))
        return AuditEvent(
            id=int(row["id"]),
            dot_id=row["dot_id"],
            at=_dt(row["at"]),
            actor=row["actor"],
            kind=row["kind"],
            tool=row["tool"],
            effect=row["effect"],
            decision=row["decision"],
            verdict=row["verdict"],
            detail=row["detail"],
        )

    def list_audit(
        self,
        dot_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
        turn_id: str | None = None,
        actors: Sequence[str] | None = None,
    ) -> list[AuditEvent]:
        self.get_dot(dot_id)
        names = list(actors) if actors is not None else None
        with self.pool.connection() as conn:
            ids = conn.execute(
                "SELECT id FROM audit_log WHERE dot_id=%s AND id>%s "
                "AND (%s::text IS NULL OR detail->>'turn_id'=%s) "
                "AND (%s::text[] IS NULL OR actor = ANY(%s::text[])) ORDER BY id LIMIT %s",
                (dot_id, after_id, turn_id, turn_id, names, names, limit),
            ).fetchall()
        return [self.get_audit(int(row[0])) for row in ids]

    def update_audit(self, event: AuditEvent) -> None:
        self.get_audit(event.id)
        try:
            with self.pool.connection() as conn:
                conn.execute("UPDATE audit_log SET actor = %s WHERE id = %s", (event.actor, event.id))
        except psycopg.Error as exc:
            if "append-only" in str(exc):
                raise AppendOnly("audit_log is append-only") from exc
            raise

    def insert_finding(self, finding: Finding) -> Finding:
        row = self._insert(
            "INSERT INTO findings (dot_id, schedule, title, evidence, score, status, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                finding.dot_id,
                finding.schedule,
                finding.title,
                Jsonb(finding.evidence),
                finding.score,
                finding.status,
                finding.created_at,
            ),
        )
        return self.get_finding(int(row["id"]))

    def get_finding(self, finding_id: int) -> Finding:
        row = self._one("SELECT * FROM findings WHERE id = %s", (finding_id,), "findings", str(finding_id))
        return Finding(
            id=int(row["id"]),
            dot_id=row["dot_id"],
            schedule=row["schedule"],
            title=row["title"],
            evidence=row["evidence"],
            score=float(row["score"]),
            status=row["status"],
            created_at=_dt(row["created_at"]),
        )

    def list_findings(self, dot_id: str, status: str | None = None) -> list[Finding]:
        self.get_dot(dot_id)
        with self.pool.connection() as conn:
            ids = conn.execute(
                "SELECT id FROM findings WHERE dot_id = %s AND (%s::text IS NULL OR status = %s) "
                "ORDER BY created_at DESC, id DESC",
                (dot_id, status, status),
            ).fetchall()
        return [self.get_finding(int(row[0])) for row in ids]

    def update_finding(self, finding: Finding) -> None:
        self._must(
            "UPDATE findings SET schedule = %s, title = %s, evidence = %s, score = %s, status = %s,"
            " created_at = %s WHERE id = %s",
            (
                finding.schedule,
                finding.title,
                Jsonb(finding.evidence),
                finding.score,
                finding.status,
                finding.created_at,
                finding.id,
            ),
            "findings",
            str(finding.id),
        )

    def insert_episode(self, episode: Episode) -> Episode:
        row = self._insert(
            "INSERT INTO episodes (dot_id, at, task, proposal, human_action, outcome)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                episode.dot_id,
                episode.at,
                episode.task,
                Jsonb(episode.proposal),
                episode.human_action,
                Jsonb(episode.outcome),
            ),
        )
        return self.get_episode(int(row["id"]))

    def get_episode(self, episode_id: int) -> Episode:
        row = self._one("SELECT * FROM episodes WHERE id = %s", (episode_id,), "episodes", str(episode_id))
        return Episode(
            id=int(row["id"]),
            dot_id=row["dot_id"],
            at=_dt(row["at"]),
            task=row["task"],
            proposal=row["proposal"],
            human_action=row["human_action"],
            outcome=row["outcome"],
        )

    def update_episode(self, episode: Episode) -> None:
        self._must(
            "UPDATE episodes SET at = %s, task = %s, proposal = %s, human_action = %s, outcome = %s WHERE id = %s",
            (
                episode.at,
                episode.task,
                Jsonb(episode.proposal),
                episode.human_action,
                Jsonb(episode.outcome),
                episode.id,
            ),
            "episodes",
            str(episode.id),
        )

    def list_episodes(
        self, dot_id: str, *, after_id: int = 0, before: datetime | None = None, limit: int = 100
    ) -> list[Episode]:
        self.get_dot(dot_id)
        with self.pool.connection() as conn:
            ids = conn.execute(
                "SELECT id FROM episodes WHERE dot_id = %s AND id > %s AND (%s::timestamptz IS NULL OR at < %s)"
                " ORDER BY id LIMIT %s",
                (dot_id, after_id, before, before, limit),
            ).fetchall()
        return [self.get_episode(int(row[0])) for row in ids]

    def insert_memory_version(self, version: MemoryVersion) -> MemoryVersion:
        row = self._insert(
            "INSERT INTO memory_versions (dot_id, at, diff, episodes, status, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (version.dot_id, version.at, version.diff, version.episodes, version.status, Jsonb(version.detail)),
        )
        return self.get_memory_version(int(row["id"]))

    def get_memory_version(self, version_id: int) -> MemoryVersion:
        row = self._one(
            "SELECT * FROM memory_versions WHERE id = %s",
            (version_id,),
            "memory_versions",
            str(version_id),
        )
        return MemoryVersion(
            id=int(row["id"]),
            dot_id=row["dot_id"],
            at=_dt(row["at"]),
            diff=row["diff"],
            episodes=[int(n) for n in row["episodes"]],
            status=row["status"],
            detail=row["detail"],
        )

    def update_memory_version(self, version: MemoryVersion) -> None:
        self._must(
            "UPDATE memory_versions SET at = %s, diff = %s, episodes = %s, status = %s, detail = %s WHERE id = %s",
            (version.at, version.diff, version.episodes, version.status, Jsonb(version.detail), version.id),
            "memory_versions",
            str(version.id),
        )

    def list_memory_versions(self, dot_id: str) -> list[MemoryVersion]:
        self.get_dot(dot_id)
        with self.pool.connection() as conn:
            ids = conn.execute(
                "SELECT id FROM memory_versions WHERE dot_id = %s ORDER BY at DESC, id DESC", (dot_id,)
            ).fetchall()
        return [self.get_memory_version(int(row[0])) for row in ids]

    def bind_channel(self, binding: ChannelBinding) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO channel_bindings (dot_id, channel, external_id) VALUES (%s, %s, %s)",
                (binding.dot_id, binding.channel, binding.external_id),
            )

    def get_channel(self, dot_id: str, channel: str) -> ChannelBinding:
        row = self._one(
            "SELECT * FROM channel_bindings WHERE dot_id = %s AND channel = %s",
            (dot_id, channel),
            "channel_bindings",
            f"{dot_id}/{channel}",
        )
        return ChannelBinding(dot_id=row["dot_id"], channel=row["channel"], external_id=row["external_id"])

    def update_channel(self, binding: ChannelBinding) -> None:
        self._must(
            "UPDATE channel_bindings SET external_id = %s WHERE dot_id = %s AND channel = %s",
            (binding.external_id, binding.dot_id, binding.channel),
            "channel_bindings",
            f"{binding.dot_id}/{binding.channel}",
        )

    def find_channel(self, channel: str, external_id: str) -> ChannelBinding:
        row = self._one(
            "SELECT * FROM channel_bindings WHERE channel = %s AND external_id = %s",
            (channel, external_id),
            "channel_bindings",
            f"{channel}/{external_id}",
        )
        return ChannelBinding(dot_id=row["dot_id"], channel=row["channel"], external_id=row["external_id"])

    def find_user_by_slack(self, slack_user_id: str) -> User:
        row = self._one("SELECT * FROM users WHERE slack_user_id = %s", (slack_user_id,), "users", slack_user_id)
        return User(
            user_id=row["user_id"],
            display_name=row["display_name"],
            slack_user_id=row["slack_user_id"],
            web_subject=row["web_subject"],
        )

    def find_user_by_web_subject(self, web_subject: str) -> User:
        row = self._one("SELECT user_id FROM users WHERE web_subject = %s", (web_subject,), "users", web_subject)
        return self.get_user(str(row["user_id"]))

    def list_dots_for_owner(self, owner_user_id: str) -> list[Dot]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            ids = cur.execute(
                "SELECT dot_id FROM dots WHERE owner_user_id = %s ORDER BY created_at, dot_id", (owner_user_id,)
            ).fetchall()
        return [self.get_dot(str(row["dot_id"])) for row in ids]

    def list_active_dots(self, pack_name: str) -> list[Dot]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            ids = cur.execute(
                "SELECT dot_id FROM dots WHERE pack_name = %s AND status = 'active' ORDER BY created_at, dot_id",
                (pack_name,),
            ).fetchall()
        return [self.get_dot(str(row["dot_id"])) for row in ids]

    def insert_schedule_run(self, message: InboxMessage) -> InboxMessage | None:
        name, slot = _schedule_key(message)
        with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            # Serialises two firings of one schedule, such as a Cloud Scheduler retry.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"schedule:{message.dot_id}:{name}",))
            existing = cur.execute(
                "SELECT 1 FROM inbox WHERE dot_id = %s AND source = 'schedule' AND payload->>'schedule' = %s"
                " AND (done_at IS NULL OR payload->>'slot' = %s) LIMIT 1",
                (message.dot_id, name, slot),
            ).fetchone()
            if existing is not None:
                return None
            row = cur.execute(
                "INSERT INTO inbox (dot_id, source, payload, profile, created_at) VALUES (%s, 'schedule', %s, %s, %s)"
                " RETURNING id",
                (message.dot_id, Jsonb(message.payload), message.profile, message.created_at),
            ).fetchone()
        if row is None:
            raise RuntimeError("insert returned no row")
        return self.get_inbox(int(row["id"]))

    def _one(self, sql: str, params: tuple[Any, ...], table: str, key: str) -> dict[str, Any]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(table, key)
        return row

    def _must(self, sql: str, params: tuple[Any, ...], table: str, key: str) -> None:
        with self.pool.connection() as conn:
            updated = conn.execute(sql, params)
            if updated.rowcount == 0:
                raise NotFound(table, key)

    def _insert(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(sql, params).fetchone()
        if row is None:
            raise RuntimeError("insert returned no row")
        return row


def make_pool(database_url: str) -> ConnectionPool:
    return ConnectionPool(conninfo=database_url, min_size=1, max_size=10, open=True)


def open_repositories(database_url: str | None) -> Repositories:
    if not database_url:
        return MemoryRepositories()
    pool = make_pool(database_url)
    migrate(pool)
    return PostgresRepositories(pool)
