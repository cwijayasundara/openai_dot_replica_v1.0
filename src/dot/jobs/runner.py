"""Claims background jobs and runs each as its own Deep Agent. See design 4.3.

The worker runs this on its own threads, apart from the inbox loop, so a dot
keeps answering while its jobs work. A finished job's result is stored as an
artifact and a ``job_result`` row goes to the dot's inbox under the profile
the job was started with.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from dot.assembly import GraphRuntime, build_job_agent, job_sandbox_key
from dot.config import Settings
from dot.jobs.store import JobStore
from dot.middleware.redaction import Redactor
from dot.persistence.db import ApprovalConflict, Dot, Job, Json, Repositories
from dot.runtime.turns import EventChannel, TurnEvent
from dot.safety.approvals import persist_job_interrupts, resume_job_command
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps


@dataclass(frozen=True)
class JobOutcome:
    status: Literal["succeeded", "failed", "cancelled", "paused"]
    text: str = ""
    error: str | None = None
    # The graph state a paused job waits in, for its approval cards.
    snapshot: Any = None


JobExecutor = Callable[[Dot, Job], JobOutcome]


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        repos: Repositories,
        events: EventChannel,
        execute: JobExecutor,
        artifacts: Callable[[str], ArtifactStore],
        redactor: Redactor | None = None,
    ) -> None:
        self._store = store
        self._repos = repos
        self._events = events
        self._execute = execute
        self._artifacts = artifacts
        self._redactor = redactor if redactor is not None else Redactor()
        self._redact = self._redactor.text

    def run_once(self) -> bool:
        """Claim and run one job. False when no job is waiting."""
        job = self._store.claim()
        if job is None:
            return False
        try:
            try:
                outcome = self._execute(self._repos.get_dot(job.dot_id), job)
            except Exception as exc:
                outcome = JobOutcome("failed", error=str(exc) or type(exc).__name__)
            self._finish(job, outcome)
        finally:
            self._store.release(job.job_id)
        return True

    def _finish(self, job: Job, outcome: JobOutcome) -> None:
        if outcome.status == "cancelled":
            return
        if outcome.status == "paused":
            # Nothing is posted: the job resumes once its review is decided.
            persist_job_interrupts(self._repos, job, outcome.snapshot, self._events, redactor=self._redactor)
            return
        result_ref = None
        if outcome.status == "succeeded":
            result_ref = self._artifacts(job.dot_id).put_text(self._redact(outcome.text))
        error = self._redact(outcome.error) if outcome.error is not None else None
        notify = result_payload(job, outcome.status, result_ref)
        if self._store.finish(job.job_id, outcome.status, result_ref=result_ref, error=error, notify=notify):
            detail: Json = {"job_id": job.job_id, "status": outcome.status, "result_ref": result_ref}
            self._events.publish(TurnEvent(job.dot_id, "job_finished", detail))


def result_payload(job: Job, status: Literal["succeeded", "failed"], result_ref: str | None) -> Json:
    """The ``job_result`` inbox payload.

    The text is a fixed template. Job output reaches the supervisor only
    through tools, never as an inbound message it could take as the user's.
    """
    if status == "succeeded":
        text = f"Background job {job.job_id} ({job.subagent}) succeeded. Result artifact: {result_ref}."
    else:
        text = f"Background job {job.job_id} ({job.subagent}) failed. The error is recorded on the job."
    return {
        "text": text,
        "job_id": job.job_id,
        "subagent": job.subagent,
        "status": status,
        "result_ref": result_ref,
        "origin": job.origin,
    }


def run_job(
    dot: Dot,
    job: Job,
    store: JobStore,
    *,
    settings: Settings,
    runtime: GraphRuntime,
    model: BaseChatModel | None = None,
    deps: ToolDeps | None = None,
) -> JobOutcome:
    """Run ``job`` on its own thread until it finishes, pauses for review or is cancelled.

    A paused job resumes here, with the decided review, when it is claimed again.
    """
    agent = build_job_agent(
        dot,
        job,
        lambda: store.current(job.job_id),
        settings=settings,
        runtime=runtime,
        model=model,
        deps=deps,
    )
    config: RunnableConfig = {"configurable": {"thread_id": job.thread_id}}
    snapshot = agent.get_state(config)
    try:
        incoming: Any = None
        if snapshot.interrupts:
            # Only a decided review resumes; an undecided one pauses (again) below.
            repos = runtime.audit_repositories
            if job.pending_run_ref is not None and repos is not None:
                with contextlib.suppress(ApprovalConflict):
                    incoming = resume_job_command(repos, job, snapshot)
        elif not snapshot.values.get("messages"):
            incoming = {"messages": [HumanMessage(content=job.instructions)]}
        # A job a dead worker left mid-run resumes from its checkpoint;
        # one that had already ended is not run again.
        if incoming is not None or (snapshot.next and not snapshot.interrupts):
            for _ in agent.stream(incoming, config, stream_mode="updates"):
                pass
            snapshot = agent.get_state(config)
    finally:
        # A paused job keeps its /work for when the review is decided.
        runtime.close_sandbox(job_sandbox_key(job), keep_work=bool(snapshot.interrupts))
    if store.current(job.job_id).status == "cancelled":
        return JobOutcome("cancelled")
    if snapshot.interrupts:
        return JobOutcome("paused", snapshot=snapshot)
    text = _final_text(snapshot.values.get("messages", []))
    if not text:
        return JobOutcome("failed", error="the job ended without a result")
    return JobOutcome("succeeded", text=text)


def _final_text(messages: list[Any]) -> str:
    last = next((message for message in reversed(messages) if isinstance(message, AIMessage)), None)
    if last is None or last.tool_calls:
        return ""
    return last.text.strip()
