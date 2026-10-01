"""Background jobs over the ``jobs`` table. See design 4.3.

Every state change is one conditional write, so cancel, finish and update
cannot overwrite each other: only ``queued`` or ``running`` jobs change, and
``finish`` only completes a job that is still ``running``.

A runner holds the job's claim for the whole run. In Postgres that claim is
an advisory lock on a dedicated connection, so a ``running`` job whose lock
is free belonged to a worker that died, and the next claim picks it up again.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from dot.persistence.db import InboxMessage, Job, Json, MemoryRepositories, NotFound, job_from_row
from dot.runtime.locks import DotLocks

JobStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
Outcome = Literal["succeeded", "failed"]
# Open jobs take updates and can be cancelled; a paused one waits on human review.
OPEN: frozenset[str] = frozenset({"queued", "running", "paused"})
# Only these are claimed: a paused job returns to queued when its review is decided.
CLAIMABLE: frozenset[str] = frozenset({"queued", "running"})
STATUSES: frozenset[str] = OPEN | {"succeeded", "failed", "cancelled"}


class JobClosed(Exception):
    """The job already finished or was cancelled."""

    def __init__(self, job: Job) -> None:
        super().__init__(f"job {job.job_id} is {job.status}")
        self.job = job


class JobStore(Protocol):
    def create(
        self, dot_id: str, subagent: str, instructions: str, *, profile: str, origin: Mapping[str, Any]
    ) -> Job: ...

    def get(self, dot_id: str, job_id: str) -> Job:
        """A job of this dot. Another dot's job is ``NotFound``."""
        ...

    def list_jobs(self, dot_id: str, status: JobStatus | None = None) -> list[Job]: ...

    def append_update(self, dot_id: str, job_id: str, message: str) -> Job:
        """Queue an instruction the job reads before its next model call."""
        ...

    def cancel(self, dot_id: str, job_id: str) -> Job:
        """Cancel an open job and close any review it waits on. A finished job is returned unchanged."""
        ...

    def claim(self) -> Job | None:
        """Take the oldest open job no live runner holds and mark it running."""
        ...

    def release(self, job_id: str) -> None: ...

    def current(self, job_id: str) -> Job: ...

    def finish(self, job_id: str, outcome: Outcome, *, result_ref: str | None, error: str | None, notify: Json) -> bool:
        """Complete a running job and post ``notify`` to the dot's inbox as ``job_result``.

        Both happen or neither. False when the job is no longer running.
        """
        ...


def new_job(
    dot_id: str, thread_id: str, subagent: str, instructions: str, profile: str, origin: Mapping[str, Any]
) -> Job:
    job_id = "job_" + uuid.uuid4().hex[:16]
    return Job(
        job_id=job_id,
        dot_id=dot_id,
        subagent=subagent,
        instructions=instructions,
        status="queued",
        thread_id=f"{thread_id}:{job_id}",
        updates=[],
        created_at=datetime.now(UTC),
        profile=profile,
        origin=dict(origin),
    )


def _update(message: str) -> Json:
    return {"message": message, "at": datetime.now(UTC).isoformat()}


def _check_status(status: str | None) -> None:
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown job status {status!r}")


class MemoryJobStore:
    def __init__(self, repos: MemoryRepositories) -> None:
        self._repos = repos
        self._lock = Lock()
        self._held: set[str] = set()

    def create(self, dot_id: str, subagent: str, instructions: str, *, profile: str, origin: Mapping[str, Any]) -> Job:
        dot = self._repos.get_dot(dot_id)
        job = new_job(dot_id, dot.thread_id, subagent, instructions, profile, origin)
        with self._lock:
            self._repos.create_job(job)
        return job

    def get(self, dot_id: str, job_id: str) -> Job:
        job = self._repos.get_job(job_id)
        if job.dot_id != dot_id:
            raise NotFound("jobs", job_id)
        return job

    def list_jobs(self, dot_id: str, status: JobStatus | None = None) -> list[Job]:
        _check_status(status)
        jobs = [job for job in self._repos.jobs.values() if job.dot_id == dot_id]
        if status is not None:
            jobs = [job for job in jobs if job.status == status]
        return sorted(jobs, key=lambda job: (job.created_at, job.job_id))

    def append_update(self, dot_id: str, job_id: str, message: str) -> Job:
        with self._lock:
            job = self.get(dot_id, job_id)
            if job.status not in OPEN:
                raise JobClosed(job)
            updated = replace(job, updates=[*job.updates, _update(message)])
            self._repos.update_job(updated)
            return updated

    def cancel(self, dot_id: str, job_id: str) -> Job:
        with self._lock:
            job = self.get(dot_id, job_id)
            if job.status not in OPEN:
                return job
            cancelled = replace(job, status="cancelled", finished_at=datetime.now(UTC))
            self._repos.update_job(cancelled)
            if job.pending_run_ref is not None:
                for card in self._repos.list_approvals(dot_id, job.pending_run_ref):
                    if card.status == "pending":
                        self._repos.update_approval(replace(card, status="cancelled"))
            return cancelled

    def claim(self) -> Job | None:
        with self._lock:
            open_jobs = sorted(
                (job for job in self._repos.jobs.values() if job.status in CLAIMABLE and job.job_id not in self._held),
                key=lambda job: (job.created_at, job.job_id),
            )
            if not open_jobs:
                return None
            job = open_jobs[0]
            running = replace(job, status="running", started_at=job.started_at or datetime.now(UTC))
            self._repos.update_job(running)
            self._held.add(job.job_id)
            return running

    def release(self, job_id: str) -> None:
        with self._lock:
            self._held.discard(job_id)

    def current(self, job_id: str) -> Job:
        return self._repos.get_job(job_id)

    def finish(self, job_id: str, outcome: Outcome, *, result_ref: str | None, error: str | None, notify: Json) -> bool:
        with self._lock:
            job = self._repos.get_job(job_id)
            if job.status != "running":
                return False
            done = replace(job, status=outcome, result_ref=result_ref, error=error, finished_at=datetime.now(UTC))
            self._repos.insert_inbox(_result_row(done, notify))
            self._repos.update_job(done)
            return True


class PostgresJobStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        # Runner threads share one store. Advisory locks are re-entrant per
        # session, so jobs held here are skipped explicitly.
        self._claims = Lock()
        self._held: set[str] = set()
        self._locks = DotLocks(pool)

    def create(self, dot_id: str, subagent: str, instructions: str, *, profile: str, origin: Mapping[str, Any]) -> Job:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT thread_id FROM dots WHERE dot_id = %s", (dot_id,)).fetchone()
            if row is None:
                raise NotFound("dots", dot_id)
            job = new_job(dot_id, str(row["thread_id"]), subagent, instructions, profile, origin)
            cur.execute(
                "INSERT INTO jobs (job_id, dot_id, subagent, instructions, status, thread_id, updates, created_at,"
                " profile, origin) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    job.job_id,
                    job.dot_id,
                    job.subagent,
                    job.instructions,
                    job.status,
                    job.thread_id,
                    Jsonb(job.updates),
                    job.created_at,
                    job.profile,
                    Jsonb(job.origin),
                ),
            )
        return job

    def get(self, dot_id: str, job_id: str) -> Job:
        row = self._row("SELECT * FROM jobs WHERE job_id = %s AND dot_id = %s", (job_id, dot_id))
        if row is None:
            raise NotFound("jobs", job_id)
        return job_from_row(row)

    def list_jobs(self, dot_id: str, status: JobStatus | None = None) -> list[Job]:
        _check_status(status)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT * FROM jobs WHERE dot_id = %s AND (%s::text IS NULL OR status = %s)"
                " ORDER BY created_at, job_id",
                (dot_id, status, status),
            ).fetchall()
        return [job_from_row(row) for row in rows]

    def append_update(self, dot_id: str, job_id: str, message: str) -> Job:
        row = self._row(
            "UPDATE jobs SET updates = updates || jsonb_build_array(%s::jsonb)"
            " WHERE job_id = %s AND dot_id = %s AND status IN ('queued', 'running', 'paused') RETURNING *",
            (Jsonb(_update(message)), job_id, dot_id),
        )
        if row is None:
            raise JobClosed(self.get(dot_id, job_id))
        return job_from_row(row)

    def cancel(self, dot_id: str, job_id: str) -> Job:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            # The row lock orders this against an approval decision for the same job.
            row = cur.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = now()"
                " WHERE job_id = %s AND dot_id = %s AND status IN ('queued', 'running', 'paused') RETURNING *",
                (job_id, dot_id),
            ).fetchone()
            if row is not None and row["pending_run_ref"] is not None:
                cur.execute(
                    "UPDATE approvals SET status = 'cancelled'"
                    " WHERE dot_id = %s AND run_ref = %s AND status = 'pending'",
                    (dot_id, row["pending_run_ref"]),
                )
        return job_from_row(row) if row is not None else self.get(dot_id, job_id)

    def claim(self) -> Job | None:
        with self._pool.connection() as conn:
            candidates = [
                str(row[0])
                for row in conn.execute(
                    "SELECT job_id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at, job_id"
                ).fetchall()
            ]
        with self._claims:
            for job_id in candidates:
                # Lock first, then recheck: a cancel between the scan and here wins.
                if job_id in self._held or not self._locks.try_acquire(_lock_key(job_id)):
                    continue
                row = self._row(
                    "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, now())"
                    " WHERE job_id = %s AND status IN ('queued', 'running') RETURNING *",
                    (job_id,),
                )
                if row is not None:
                    self._held.add(job_id)
                    return job_from_row(row)
                self._locks.release(_lock_key(job_id))
        return None

    def release(self, job_id: str) -> None:
        with self._claims:
            self._held.discard(job_id)
            self._locks.release(_lock_key(job_id))

    def current(self, job_id: str) -> Job:
        row = self._row("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
        if row is None:
            raise NotFound("jobs", job_id)
        return job_from_row(row)

    def finish(self, job_id: str, outcome: Outcome, *, result_ref: str | None, error: str | None, notify: Json) -> bool:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "UPDATE jobs SET status = %s, result_ref = %s, error = %s, finished_at = now()"
                " WHERE job_id = %s AND status = 'running' RETURNING *",
                (outcome, result_ref, error, job_id),
            ).fetchone()
            if row is None:
                return False
            message = _result_row(job_from_row(row), notify)
            cur.execute(
                "INSERT INTO inbox (dot_id, source, payload, profile, created_at) VALUES (%s, %s, %s, %s, %s)",
                (message.dot_id, message.source, Jsonb(message.payload), message.profile, message.created_at),
            )
        return True

    def _row(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(sql, params).fetchone()


def _lock_key(job_id: str) -> str:
    # Dot locks share the advisory key space; the prefix keeps them apart.
    return f"job:{job_id}"


def _result_row(job: Job, notify: Json) -> InboxMessage:
    return InboxMessage(
        id=0,
        dot_id=job.dot_id,
        source="job_result",
        payload=dict(notify),
        profile=job.profile,
        created_at=datetime.now(UTC),
    )
