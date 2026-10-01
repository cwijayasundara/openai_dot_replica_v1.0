"""Background jobs run as their own Deep Agent, with the scripted model."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from dot.assembly import GraphRuntime, build_graph_runtime
from dot.config import Settings
from dot.jobs.runner import JobOutcome, JobRunner, run_job
from dot.jobs.store import MemoryJobStore
from dot.persistence.db import Dot, InboxMessage, Job, MemoryRepositories, User
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.job_store_contract import WHEN
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

INSTRUCTION = "Research open dot runtimes and draft a note to Sam."
ORIGIN = {"source": "slack", "inbox_ids": [3], "instruction": INSTRUCTION}


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        del limit
        self.queries.append(query)
        return [Hit(title="Open Dot", url="https://example.com/dot", snippet="A runtime.")]


@dataclass
class Rig:
    repos: MemoryRepositories
    store: MemoryJobStore
    runtime: GraphRuntime
    settings: Settings
    search: _Search
    events: InMemoryEventChannel
    root: Path

    def runner(self, model: ScriptedChatModel) -> JobRunner:
        deps = ToolDeps(ArtifactStore(self.root / "tool-artifacts"), search=self.search)
        return JobRunner(
            self.store,
            self.repos,
            self.events,
            lambda dot, job: run_job(
                dot, job, self.store, settings=self.settings, runtime=self.runtime, model=model, deps=deps
            ),
            lambda dot_id: ArtifactStore(self.root / "results" / dot_id),
        )

    def results(self) -> list[InboxMessage]:
        return [m for m in self.repos.inbox.values() if m.source == "job_result"]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    repos = MemoryRepositories()
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", WHEN))
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    try:
        yield Rig(repos, MemoryJobStore(repos), runtime, settings, _Search(), InMemoryEventChannel(), tmp_path)
    finally:
        runtime.close()


def _texts(messages: list[BaseMessage], needle: str) -> int:
    return sum(1 for m in messages if needle in str(m.content))


def test_a_job_runs_on_its_own_thread_and_reports_to_the_inbox(rig: Rig) -> None:
    job = rig.store.create("dot-1", "researcher", "Find open dot runtimes.", profile="chat", origin=ORIGIN)
    model = ScriptedChatModel(script=[tools(call("web_search", query="open dot")), say("Brief: one runtime found.")])

    assert rig.runner(model).run_once()
    assert rig.runner(model).run_once() is False

    done = rig.store.current(job.job_id)
    assert done.status == "succeeded" and done.result_ref is not None
    assert ArtifactStore(rig.root / "results" / "dot-1").read_text(done.result_ref) == "Brief: one runtime found."
    assert rig.search.queries == ["open dot"]
    assert model.seen[0][-1].content == "Find open dot runtimes."
    # Its own thread: the dot's thread has no checkpoint.
    assert rig.runtime.checkpointer.get_tuple({"configurable": {"thread_id": "thread-1"}}) is None
    assert rig.runtime.checkpointer.get_tuple({"configurable": {"thread_id": job.thread_id}}) is not None
    [row] = rig.results()
    assert row.profile == "chat"
    assert row.payload["origin"] == ORIGIN and row.payload["result_ref"] == done.result_ref
    assert "Brief" not in row.payload["text"]  # job output never arrives as an inbound message
    assert [e.kind for e in rig.events.events] == ["job_finished"]
    audit_actors = {event.actor for event in rig.repos.audit.values()}
    assert audit_actors == {"researcher"}


def test_updates_reach_the_next_model_call_once(rig: Rig) -> None:
    job = rig.store.create("dot-1", "researcher", "Find runtimes.", profile="chat", origin=ORIGIN)

    def search_and_update(messages: list[BaseMessage]) -> object:
        del messages
        rig.store.append_update("dot-1", job.job_id, "Only cover 2026.")
        return tools(call("web_search", query="runtimes"))

    model = ScriptedChatModel(
        script=[search_and_update, tools(call("web_search", query="runtimes 2026")), say("Brief for 2026.")]
    )
    assert rig.runner(model).run_once()

    assert _texts(model.seen[0], "Only cover 2026.") == 0
    assert _texts(model.seen[1], "[update from the dot] Only cover 2026.") == 1
    assert _texts(model.seen[2], "[update from the dot] Only cover 2026.") == 1
    assert rig.store.current(job.job_id).status == "succeeded"


def test_cancel_stops_within_one_step(rig: Rig) -> None:
    job = rig.store.create("dot-1", "researcher", "Find runtimes.", profile="chat", origin=ORIGIN)

    def cancel_then_search(messages: list[BaseMessage]) -> object:
        del messages
        rig.store.cancel("dot-1", job.job_id)
        return tools(call("web_search", query="runtimes"))

    model = ScriptedChatModel(script=[cancel_then_search, say("never reached")])
    assert rig.runner(model).run_once()

    assert model.calls == 1
    assert rig.search.queries == []
    current = rig.store.current(job.job_id)
    assert current.status == "cancelled" and current.result_ref is None
    assert rig.results() == [] and rig.events.events == []


def test_a_crashing_job_fails_and_reports(rig: Rig) -> None:
    job = rig.store.create("dot-1", "researcher", "Find runtimes.", profile="chat", origin=ORIGIN)

    def crash(dot: Dot, claimed: Job) -> JobOutcome:
        raise RuntimeError("sandbox down")

    runner = JobRunner(rig.store, rig.repos, rig.events, crash, lambda d: ArtifactStore(rig.root / d))
    assert runner.run_once()

    failed = rig.store.current(job.job_id)
    assert failed.status == "failed" and failed.error == "sandbox down"
    [row] = rig.results()
    assert row.payload["status"] == "failed" and "sandbox down" not in row.payload["text"]
    # The claim was released: nothing is left to run.
    assert runner.run_once() is False


def test_a_failing_model_fails_the_job_instead_of_becoming_its_result(rig: Rig) -> None:
    job = rig.store.create("dot-1", "researcher", "Find runtimes.", profile="chat", origin=ORIGIN)
    model = ScriptedChatModel(script=[])  # raises on every call, including retries

    assert rig.runner(model).run_once()

    failed = rig.store.current(job.job_id)
    assert failed.status == "failed" and "ran out of steps" in (failed.error or "")
    assert failed.result_ref is None


def test_a_job_over_its_model_call_budget_fails(rig: Rig) -> None:
    rig.settings = rig.settings.model_copy(update={"max_model_calls": 1})
    job = rig.store.create("dot-1", "researcher", "Find runtimes.", profile="chat", origin=ORIGIN)
    model = ScriptedChatModel(script=[tools(call("web_search", query="a")), say("never reached")])

    assert rig.runner(model).run_once()

    assert rig.store.current(job.job_id).status == "failed"


def test_a_reclaimed_job_resumes_from_its_checkpoint(rig: Rig) -> None:
    job = rig.store.create("dot-1", "researcher", "Find runtimes.", profile="chat", origin=ORIGIN)
    claimed = rig.store.claim()
    assert claimed is not None
    dot = rig.repos.get_dot("dot-1")
    deps = ToolDeps(ArtifactStore(rig.root / "tool-artifacts"), search=rig.search)
    # The search runs and is checkpointed; the next model call dies with the worker.
    first = ScriptedChatModel(script=[tools(call("web_search", query="runtimes"))])
    with pytest.raises(AssertionError, match="ran out of steps"):
        run_job(dot, claimed, rig.store, settings=rig.settings, runtime=rig.runtime, model=first, deps=deps)
    rig.store.release(job.job_id)  # the dead worker's claim lapses

    resumed = ScriptedChatModel(script=[say("Brief after resume.")])
    assert rig.runner(resumed).run_once()

    assert rig.search.queries == ["runtimes"]  # the finished step is not repeated
    assert resumed.calls == 1
    assert _texts(resumed.seen[0], "Find runtimes.") == 1
    assert any(m.type == "tool" and m.name == "web_search" for m in resumed.seen[0])
    done = rig.store.current(job.job_id)
    assert done.status == "succeeded"
    assert done.result_ref is not None
    assert ArtifactStore(rig.root / "results" / "dot-1").read_text(done.result_ref) == "Brief after resume."

    # A job that already ended is not run again.
    idle = ScriptedChatModel(script=[])
    again = run_job(
        rig.repos.get_dot("dot-1"), done, rig.store, settings=rig.settings, runtime=rig.runtime, model=idle, deps=deps
    )
    assert again.text == "Brief after resume." and idle.calls == 0


def test_a_subagent_outside_the_profile_fails(rig: Rig) -> None:
    job = rig.store.create("dot-1", "coder", "Write code.", profile="sweep", origin=ORIGIN)
    model = ScriptedChatModel(script=[])

    assert rig.runner(model).run_once()

    failed = rig.store.current(job.job_id)
    assert failed.status == "failed" and "not granted to profile 'sweep'" in (failed.error or "")
    assert model.calls == 0


def test_a_job_reviews_against_the_user_instruction_and_pauses_for_approval(rig: Rig) -> None:
    job = rig.store.create("dot-1", "coder", "Supervisor-written: run the script.", profile="chat", origin=ORIGIN)
    model = ScriptedChatModel(script=[tools(call("execute", command="python /work/run.py")), say("ran")])

    assert rig.runner(model).run_once()

    paused = rig.store.current(job.job_id)
    assert paused.status == "paused" and paused.pending_run_ref is not None
    assert rig.results() == []
    [review] = model.structured_seen
    data = json.loads(str(review[1].content))
    assert data["original_instruction"] == INSTRUCTION
    assert "Supervisor-written" not in str(review)


def test_a_job_result_turn_is_reviewed_against_the_originating_instruction(rig: Rig) -> None:
    dot = rig.repos.get_dot("dot-1")
    payload = {
        "text": "Background job job_1 (researcher) succeeded. Result artifact: art_0000000000000000.",
        "job_id": "job_1",
        "status": "succeeded",
        "result_ref": "art_0000000000000000",
        "origin": ORIGIN,
    }
    inbound = InboxMessage(1, dot.dot_id, "job_result", payload, "chat", WHEN)
    args = {"to": "sam@example.com", "subject": "Runtimes", "body": "Here is the brief."}
    model = ScriptedChatModel(script=[tools(call("draft_email", **args)), say("Drafted.")])
    deps = ToolDeps(ArtifactStore(rig.root / "tool-artifacts"))

    run_agent_turn(
        dot, "chat", [inbound], rig.events, settings=rig.settings, runtime=rig.runtime, model=model, deps=deps
    )

    [review] = model.structured_seen
    data = json.loads(str(review[1].content))
    assert data["original_instruction"] == INSTRUCTION
    # The model still sees the job-result notice itself.
    assert any(isinstance(m, HumanMessage) and "[job_result]" in str(m.content) for m in model.seen[0])
