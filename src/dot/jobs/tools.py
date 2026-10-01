"""The supervisor's job tools: start, check, update, cancel, list. See design 4.3.

The tools act only on the dot they were built for. ``start_job`` records the
job's origin from graph state, never from its arguments: the user's objective
is the Guardian's captured instruction for the turn, and the reply channel is
the one the worker tagged on the latest inbound message. A job's result
reaches the supervisor here, as a bounded untrusted excerpt in tool output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool

from dot.jobs.store import STATUSES, JobClosed, JobStatus, JobStore
from dot.packs.schema import SubagentSpec
from dot.persistence.db import Job, Json, NotFound
from dot.runtime.router import latest_channel
from dot.tools.artifacts import ArtifactStore
from dot.tools.effects import Effect
from dot.tools.native.envelope import envelope
from dot.tools.results import fail, ok

# Starting and steering a job changes nothing outside the dot. The job's own
# tools are gated by policy and the Guardian when they run, as with ``task``.
JOB_EFFECTS: dict[str, Effect] = {
    "start_job": Effect.read,
    "check_job": Effect.read,
    "update_job": Effect.read,
    "cancel_job": Effect.read,
    "list_jobs": Effect.read,
}
MAX_INSTRUCTIONS = 20_000
RESULT_EXCERPT = 4_000
LIST_LIMIT = 20


def build_job_tools(
    store: JobStore,
    artifacts: ArtifactStore,
    dot_id: str,
    profile: str,
    subagents: Sequence[SubagentSpec],
) -> list[BaseTool]:
    granted = {spec.name: spec.description for spec in subagents}
    menu = "; ".join(f"{name}: {description}" for name, description in sorted(granted.items()))

    def start_job(subagent: str, instructions: str, runtime: ToolRuntime) -> str:
        if subagent not in granted:
            return fail("unknown subagent", available=sorted(granted))
        if not instructions.strip():
            return fail("instructions are required")
        if len(instructions) > MAX_INSTRUCTIONS:
            return fail("instructions are too long")
        job = store.create(dot_id, subagent, instructions.strip(), profile=profile, origin=_origin(runtime.state))
        return ok(job_id=job.job_id, status=job.status)

    def check_job(job_id: str) -> str:
        """Status of a background job, its last update and, once done, an excerpt of its result."""
        try:
            job = store.get(dot_id, job_id)
        except NotFound:
            return fail("no such job", job_id=job_id)
        payload = _summary(job)
        if job.updates:
            payload["last_update"] = job.updates[-1]["message"]
        if job.result_ref is not None:
            try:
                body = artifacts.read_text(job.result_ref)
            except KeyError:
                payload["result"] = None
            else:
                payload["result"] = envelope(body, source=f"job:{job.job_id}", limit=RESULT_EXCERPT)
        if job.error is not None:
            payload["error"] = envelope(job.error, source=f"job:{job.job_id}", limit=500)
        return ok(**payload)

    def update_job(job_id: str, message: str) -> str:
        """Send a running or queued job an instruction it reads before its next step."""
        if not message.strip():
            return fail("message is required")
        if len(message) > MAX_INSTRUCTIONS:
            return fail("message is too long")
        try:
            job = store.append_update(dot_id, job_id, message.strip())
        except NotFound:
            return fail("no such job", job_id=job_id)
        except JobClosed as closed:
            return fail(f"job is {closed.job.status}", job_id=job_id)
        return ok(job_id=job.job_id, status=job.status, updates=len(job.updates))

    def cancel_job(job_id: str) -> str:
        """Cancel a background job. It stops before its next model or tool step."""
        try:
            job = store.cancel(dot_id, job_id)
        except NotFound:
            return fail("no such job", job_id=job_id)
        return ok(job_id=job.job_id, status=job.status)

    def list_jobs(status: str | None = None) -> str:
        """This dot's most recent background jobs, optionally only those with one status."""
        if status is not None and status not in STATUSES:
            return fail("unknown status", known=sorted(STATUSES))
        chosen: JobStatus | None = status  # type: ignore[assignment]
        jobs = store.list_jobs(dot_id, chosen)[-LIST_LIMIT:]
        return ok(jobs=[_summary(job) for job in jobs])

    return [
        StructuredTool.from_function(
            start_job,
            name="start_job",
            description=(
                "Start a background job and return its job_id at once. The job runs while you keep answering; "
                f"when it ends you receive a job_result message. Subagents: {menu}."
            ),
        ),
        StructuredTool.from_function(check_job, name="check_job"),
        StructuredTool.from_function(update_job, name="update_job"),
        StructuredTool.from_function(cancel_job, name="cancel_job"),
        StructuredTool.from_function(list_jobs, name="list_jobs"),
    ]


def _summary(job: Job) -> Json:
    return {
        "job_id": job.job_id,
        "subagent": job.subagent,
        "status": job.status,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "result_ref": job.result_ref,
    }


def _origin(state: Any) -> Json:
    values: Mapping[str, Any] = state if isinstance(state, Mapping) else {}
    instruction = values.get("guardian_instruction")
    origin: Json = {"instruction": instruction if isinstance(instruction, str) else ""}
    turn_id = values.get("audit_turn_id")
    if isinstance(turn_id, str):
        origin["turn_id"] = turn_id
    channel = latest_channel(values.get("messages", []))
    if channel is not None:
        origin["channel"] = channel
    return origin
