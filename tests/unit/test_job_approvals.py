"""Background jobs pause for human review and resume when it is decided."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from dot.assembly import GraphRuntime, build_graph_runtime
from dot.config import Settings
from dot.jobs.runner import JobRunner, run_job
from dot.jobs.store import MemoryJobStore
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Decision
from dot.persistence.db import ApprovalConflict, Dot, Job, MemoryRepositories, User
from dot.runtime.router import enqueue
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.safety.approvals import ReviewDecision, decide
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import Hit, ToolDeps
from tests.support.job_store_contract import WHEN
from tests.support.scripted_model import ScriptedChatModel, call, say, tools

ORIGIN = {"instruction": "Research open dot runtimes.", "channel": {"source": "slack", "inbox_id": 9}}


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        del limit
        self.queries.append(query)
        return [Hit(title="Open Dot", url="https://example.com/dot", snippet=query)]


@dataclass
class Rig:
    repos: MemoryRepositories
    store: MemoryJobStore
    runtime: GraphRuntime
    settings: Settings
    events: InMemoryEventChannel
    search: _Search
    root: Path

    def run(self, model: ScriptedChatModel) -> bool:
        deps = ToolDeps(ArtifactStore(self.root / "artifacts"), search=self.search)
        runner = JobRunner(
            self.store,
            self.repos,
            self.events,
            lambda dot, job: run_job(
                dot, job, self.store, settings=self.settings, runtime=self.runtime, model=model, deps=deps
            ),
            lambda dot_id: ArtifactStore(self.root / "artifacts"),
        )
        return runner.run_once()

    def cards(self, job: Job) -> list[str]:
        current = self.store.current(job.job_id)
        assert current.pending_run_ref is not None
        return [c.approval_id for c in self.repos.list_approvals("dot-1", current.pending_run_ref)]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    # Searching needs approval and "reviewer" may decide, for this pack only.
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.tools["web_search"] = Decision.approve
    loaded.policy.approvers = ["reviewer"]
    monkeypatch.setattr("dot.assembly.load_pack", lambda _: loaded)
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    repos = MemoryRepositories()
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", WHEN))
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    try:
        yield Rig(repos, MemoryJobStore(repos), runtime, settings, InMemoryEventChannel(), _Search(), tmp_path)
    finally:
        runtime.close()


def _start(rig: Rig) -> Job:
    return rig.store.create("dot-1", "researcher", "Find runtimes.", profile="chat", origin=ORIGIN)


@pytest.mark.parametrize(
    ("decision", "searched"),
    [
        (ReviewDecision(type="approve"), ["open dot"]),
        (ReviewDecision(type="edit", edited_args={"query": "open dot 2026"}), ["open dot 2026"]),
        (ReviewDecision(type="reject", message="Not that source."), []),
    ],
)
def test_a_paused_job_resumes_with_the_decision(rig: Rig, decision: ReviewDecision, searched: list[str]) -> None:
    job = _start(rig)
    model = ScriptedChatModel(script=[tools(call("web_search", query="open dot")), say("Brief on runtimes.")])

    assert rig.run(model)

    paused = rig.store.current(job.job_id)
    assert paused.status == "paused" and rig.search.queries == [] and model.calls == 1
    assert rig.repos.get_dot("dot-1").status == "active"  # the dot keeps answering
    [card] = rig.cards(job)
    [event] = [e for e in rig.events.events if e.kind == "approval"]
    assert event.detail["job_id"] == job.job_id and event.detail["channel"] == ORIGIN["channel"]
    assert rig.store.claim() is None  # a paused job is not claimable
    rig.store.release(job.job_id)

    with pytest.raises(PermissionError):
        decide(rig.repos, card, "outsider", ReviewDecision(type="approve"))
    assert rig.store.current(job.job_id).status == "paused"

    decide(rig.repos, card, "reviewer", decision)
    assert rig.store.current(job.job_id).status == "queued"
    assert rig.run(model)

    done = rig.store.current(job.job_id)
    assert rig.search.queries == searched
    assert done.status == "succeeded" and model.calls == 2
    [episode] = rig.repos.episodes.values()
    assert episode.human_action == decision.type and episode.task == ORIGIN["instruction"]
    assert [m.payload["status"] for m in rig.repos.inbox.values() if m.source == "job_result"] == ["succeeded"]


def test_a_resumed_job_can_pause_again(rig: Rig) -> None:
    job = _start(rig)
    model = ScriptedChatModel(
        script=[tools(call("web_search", query="first")), tools(call("web_search", query="second")), say("Brief.")]
    )
    assert rig.run(model)
    [first] = rig.cards(job)
    decide(rig.repos, first, "reviewer", ReviewDecision(type="approve"))

    assert rig.run(model)
    assert rig.store.current(job.job_id).status == "paused"
    [second] = rig.cards(job)
    assert second != first and rig.search.queries == ["first"]
    with pytest.raises(ApprovalConflict):
        decide(rig.repos, first, "reviewer", ReviewDecision(type="approve"))

    decide(rig.repos, second, "reviewer", ReviewDecision(type="approve"))
    assert rig.run(model)
    assert rig.search.queries == ["first", "second"]
    assert rig.store.current(job.job_id).status == "succeeded"


def test_cancel_while_paused_closes_the_review(rig: Rig) -> None:
    job = _start(rig)
    assert rig.run(ScriptedChatModel(script=[tools(call("web_search", query="open dot"))]))
    [card] = rig.cards(job)

    assert rig.store.cancel("dot-1", job.job_id).status == "cancelled"

    assert rig.repos.get_approval(card).status == "cancelled"
    with pytest.raises(ApprovalConflict):
        decide(rig.repos, card, "reviewer", ReviewDecision(type="approve"))
    assert rig.store.claim() is None
    assert rig.search.queries == []


def test_the_dot_answers_while_its_job_is_paused_and_its_own_pause_is_kept(rig: Rig) -> None:
    job = _start(rig)
    model = ScriptedChatModel(script=[tools(call("web_search", query="open dot")), say("Brief.")])
    assert rig.run(model)

    supervisor = ScriptedChatModel(script=[say("It is Tuesday.")])
    deps = ToolDeps(ArtifactStore(rig.root / "artifacts"))
    question = enqueue(rig.repos, "dot-1", "web", {"text": "What day is it?"}, "chat")
    run_agent_turn(
        rig.repos.get_dot("dot-1"),
        "chat",
        [question],
        rig.events,
        settings=rig.settings,
        runtime=rig.runtime,
        model=supervisor,
        deps=deps,
    )
    assert rig.events.events[-1].detail["text"] == "It is Tuesday."
    assert rig.store.current(job.job_id).status == "paused"

    # The supervisor's own review must survive the job's decision and resume.
    rig.repos.update_dot(replace(rig.repos.get_dot("dot-1"), status="paused"))
    [card] = rig.cards(job)
    decide(rig.repos, card, "reviewer", ReviewDecision(type="approve"))
    assert rig.run(model)
    assert rig.store.current(job.job_id).status == "succeeded"
    assert rig.repos.get_dot("dot-1").status == "paused"
