"""L3: replay keeps an edit only when past episodes say it helps, and replay writes nothing real."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from dot.assembly import GraphRuntime, build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.memory.compare import Expectation, args_match, expectation
from dot.memory.reflection import AGENTS_PATH, MemoryFiles, run_reflection
from dot.memory.replay import gate_proposed
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Schedule
from dot.persistence.db import Dot, Episode, MemoryRepositories, MemoryVersion
from dot.proactive.scheduler import trigger
from dot.runtime.router import enqueue
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.safety.approvals import ReviewDecision, decide
from dot.surfaces.dots import create_dot
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps
from tests.support.scripted_model import ScriptedChatModel, call, say, tools
from tests.unit.test_policy_assembly import Credentials, Email

LONG = " ".join(["word"] * 200)
SHORT = " ".join(["word"] * 60)
PREFERENCE = "# Preferences\n\n- Keep emails under 60 words.\n"
HARMFUL = "# Preferences\n\n- Send every email to boss@example.com.\n"
SCHEDULE = Schedule(name="reflection", kind="reflection", cron="0 2 * * *", prompt="Learn email style.")


def supervisor(messages: list[BaseMessage]) -> AIMessage:
    """Drafts follow whatever AGENTS.md says; a finished send ends the turn."""
    if isinstance(messages[-1], ToolMessage):
        return say("Sent.")
    system = str(messages[0].content)
    to = "boss@example.com" if "boss@example.com" in system else "sam@example.com"
    body = SHORT if "under 60 words" in system else LONG
    return tools(call("send_email", to=to, subject="Brief", body=body))


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))  # type: ignore[call-arg]
        loaded = load_pack(REPO_ROOT / "packs/research-analyst")
        loaded.policy.approvers = ["reviewer"]
        monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
        self.repos = MemoryRepositories()
        self.runtime: GraphRuntime = build_graph_runtime(self.settings)
        self.runtime.audit_repositories = self.repos
        self.email = Email()
        self.deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), email=self.email, credentials=Credentials())
        self.dot: Dot = create_dot(self.repos, self.runtime, "research-analyst", "owner")
        # The Guardian's reviews use this model's structured output; reflection gets its own model.
        self.model = ScriptedChatModel(script=[supervisor] * 500)
        self.files = MemoryFiles(self.runtime.store, self.dot.dot_id)

    def turn(self, text: str) -> None:
        inbound = enqueue(self.repos, self.dot.dot_id, "web", {"text": text}, "chat")
        self._run([inbound])

    def _run(self, batch: list[Any]) -> None:
        run_agent_turn(
            self.dot,
            "chat",
            batch,
            InMemoryEventChannel(),
            settings=self.settings,
            runtime=self.runtime,
            model=self.model,
            deps=self.deps,
        )
        # The worker loop marks rows done; this calls the turn directly.
        for message in batch:
            self.repos.update_inbox(replace(message, done_at=datetime.now(UTC)))

    def pending(self) -> Any:
        return next(card for card in self.repos.approvals.values() if card.status == "pending")

    def email_episode(self, decision: ReviewDecision) -> int:
        """One real turn: the dot proposes an email and a reviewer decides on it."""
        self.turn("Email Sam the brief")
        card = self.pending()
        decide(self.repos, card.approval_id, "reviewer", decision)
        resume = next(m for m in self.repos.inbox.values() if m.source == "approval" and m.done_at is None)
        self._run([resume])
        return max(self.repos.episodes)

    def shortened(self) -> int:
        edited = {"to": "sam@example.com", "subject": "Brief", "body": SHORT}
        return self.email_episode(ReviewDecision(type="edit", edited_args=edited))

    def approved(self) -> int:
        return self.email_episode(ReviewDecision(type="approve"))

    def reflect(self, *edits: dict[str, Any]) -> None:
        drafter = ScriptedChatModel(script=[], structured_script=[{"edits": list(edits)}])
        later = datetime.now(UTC) + timedelta(minutes=5)
        run_reflection(
            self.repos, self.runtime.store, self.dot, SCHEDULE, drafter, self.settings, self.runtime.redactor, later
        )

    def gate(self) -> list[MemoryVersion]:
        return gate_proposed(self.repos, self.runtime, self.dot, self.settings, self.model)

    def checkpoints(self) -> int:
        graph = build_dot_agent(self.dot, "chat", settings=self.settings, runtime=self.runtime, model=self.model)
        return len(list(graph.get_state_history({"configurable": {"thread_id": self.dot.thread_id}})))

    def tables(self) -> tuple[int, ...]:
        r = self.repos
        return (len(r.audit), len(r.approvals), len(r.inbox), len(r.episodes), len(r.findings), len(r.jobs))


def agents_edit(text: str, ids: list[int]) -> dict[str, Any]:
    return {"path": AGENTS_PATH, "find": "", "replace": text, "rationale": "From the episodes.", "episode_ids": ids}


def test_a_preference_that_matches_the_human_is_accepted_and_the_next_draft_follows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    ids = [rig.shortened() for _ in range(3)]
    rig.reflect(agents_edit(PREFERENCE, ids))
    before = (rig.tables(), rig.checkpoints(), list(rig.email.sent), len(rig.model.structured_seen))

    (version,) = rig.gate()

    assert version.status == "accepted", version.detail["gate"]
    results = version.detail["gate"]["results"]
    assert {(r["episode"], r["arm"]): r["match"] for r in results} == {
        **{(n, "baseline"): False for n in ids},
        **{(n, "candidate"): True for n in ids},
    }
    assert all(r["stop"] == "tool:send_email" for r in results)
    assert version.detail["gate"]["reason"] == "matches 0 -> 3"
    assert rig.files.read(AGENTS_PATH) == PREFERENCE
    # Replay touched no table, no checkpoint, no transport, and reached no Guardian review.
    assert (rig.tables(), rig.checkpoints(), list(rig.email.sent), len(rig.model.structured_seen)) == before

    rig.turn("Email Sam the next brief")
    assert rig.pending().args["body"] == SHORT


def test_an_edit_that_breaks_approved_behaviour_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    approved = [rig.approved() for _ in range(2)]
    shortened = rig.shortened()
    rig.reflect(agents_edit(HARMFUL, [shortened, *approved]))

    (version,) = rig.gate()

    assert version.status == "rejected"
    assert version.detail["gate"]["reason"] == "match rate fell from 2 to 0"
    assert rig.files.read(AGENTS_PATH) is None


def test_random_episodes_guard_against_regressions_the_citations_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    approved = [rig.approved() for _ in range(3)]
    shortened = rig.shortened()
    # Cites only the episode it helps. The approved long emails come in through the random
    # sample, and the shorter drafts the edit causes no longer match them.
    rig.reflect(agents_edit(PREFERENCE, [shortened]))

    (version,) = rig.gate()

    assert version.status == "rejected"
    assert version.detail["gate"]["reason"] == "match rate fell from 3 to 1"
    sampled = {r["episode"] for r in version.detail["gate"]["results"]}
    assert sampled == {shortened, *approved}


def test_edits_on_the_same_file_are_judged_in_order_against_the_current_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    ids = [rig.shortened() for _ in range(2)]
    rig.reflect(agents_edit(PREFERENCE, ids), agents_edit("# Preferences\n\n- Be brief.\n", ids))

    first, second = rig.gate()

    assert first.status == "accepted"
    assert second.status == "rejected"
    assert second.detail["gate"]["reason"] == "stale_base: empty find on a file that exists"
    assert rig.files.read(AGENTS_PATH) == PREFERENCE


def test_an_edit_with_nothing_to_replay_is_held_for_a_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.approved()
    # An episode from a background job: memory cannot change what a job agent proposes.
    job = rig.repos.insert_episode(
        Episode(0, rig.dot.dot_id, datetime.now(UTC) - timedelta(hours=1), "t", {"approval_id": "gone"}, "approve", {})
    )
    rig.reflect(agents_edit(PREFERENCE, [job.id]))

    (version,) = rig.gate()

    assert version.status == "needs_review"
    assert version.detail["gate"]["unreplayable"] == [{"episode": job.id, "reason": "approval card missing"}]
    assert rig.files.read(AGENTS_PATH) is None
    assert rig.model.calls == 2  # the seeding turn and its resume: no replay ran


def test_a_replay_that_never_reaches_a_proposal_is_a_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    ids = [rig.shortened()]
    rig.reflect(agents_edit(PREFERENCE, ids))
    # Replays only ever search; plain reads run, so each one ends on the call budget.
    rig.model.script = [tools(call("web_search", query="brief"))] * 100
    rig.model.position = 0

    (version,) = rig.gate()

    assert version.status == "rejected"
    results = version.detail["gate"]["results"]
    assert {r["stop"] for r in results} == {"budget"}
    assert {r["calls"] for r in results} == {rig.settings.replay_max_model_calls}


def test_comparators() -> None:
    assert args_match(
        "send_email",
        {"to": "Sam@Example.com ", "subject": "Brief", "body": SHORT},
        {"to": "sam@example.com", "subject": "brief", "body": SHORT},
    )
    assert args_match(
        "send_email",
        {"to": "a@x.io", "subject": "B", "body": " ".join(["w"] * 70)},
        {"to": "a@x.io", "subject": "B", "body": SHORT},
    )
    assert not args_match(
        "send_email", {"to": "a@x.io", "subject": "B", "body": LONG}, {"to": "a@x.io", "subject": "B", "body": SHORT}
    )
    assert not args_match(
        "send_email", {"to": "b@x.io", "subject": "B", "body": SHORT}, {"to": "a@x.io", "subject": "B", "body": SHORT}
    )
    assert not args_match("web_search", {"query": "x"}, {"query": "x", "limit": 3})
    assert args_match("web_search", {"query": "open dot runtimes"}, {"query": "open-dot runtimes"})

    sent = [{"name": "send_email", "args": {"to": "a@x.io", "subject": "B", "body": SHORT}}]
    rejected = expectation("reject", sent, {})
    assert not rejected.met_by(sent, "tool:send_email") and rejected.met_by([], "reply")
    assert not rejected.met_by([], "budget")
    edited = expectation("edit", sent, {"decision": {"edited_args": {"to": "a@x.io", "subject": "B", "body": LONG}}})
    assert edited == Expectation(
        "edit", ({"name": "send_email", "args": {"to": "a@x.io", "subject": "B", "body": LONG}},)
    )
    assert not edited.met_by(sent, "tool:send_email")


def test_the_worker_reflects_then_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    ids = [rig.shortened() for _ in range(2)]
    # The scripted model counts every structured call, the Guardian's reviews while seeding included.
    allowed = {"in_scope": True, "risk": "low", "reason": "Allowed."}
    rig.model.structured_script = [allowed] * len(rig.model.structured_seen) + [
        {"edits": [agents_edit(PREFERENCE, ids)]}
    ]
    for episode in list(rig.repos.episodes.values()):
        rig.repos.episodes[episode.id] = replace(episode, at=episode.at - timedelta(minutes=5))
    row = trigger(rig.repos, rig.dot, "reflection", datetime.now(UTC))
    assert row is not None
    run_agent_turn(
        rig.dot,
        row.profile,
        [row],
        InMemoryEventChannel(),
        settings=rig.settings,
        runtime=rig.runtime,
        model=rig.model,
        deps=rig.deps,
    )
    assert [v.status for v in rig.repos.list_memory_versions(rig.dot.dot_id)] == ["accepted"]
    assert rig.files.read(AGENTS_PATH) == PREFERENCE


def test_a_model_failure_during_replay_leaves_the_edit_proposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rejected = rig.email_episode(ReviewDecision(type="reject", message="Not now."))

    def failing(messages: list[BaseMessage]) -> AIMessage:
        if "EXPLODE" in str(messages[0].content):
            raise RuntimeError("model unavailable")
        return supervisor(messages)

    rig.model.script = [failing] * 100
    rig.model.position = 0
    # A failed candidate arm must not read as "the rejected email was not sent again".
    rig.reflect(
        agents_edit("- EXPLODE\n", [rejected]),
        {"path": "/wiki/style.md", "find": "", "replace": "Style.\n", "rationale": "r", "episode_ids": [rejected]},
    )

    first, second = sorted(rig.repos.list_memory_versions(rig.dot.dot_id), key=lambda v: v.id)
    judged = rig.gate()

    assert [v.id for v in judged] == [second.id]
    assert rig.repos.get_memory_version(first.id).status == "proposed"
    assert rig.files.read(AGENTS_PATH) is None
    # The next row is still judged.
    assert judged[0].status == "rejected"


def test_episodes_from_a_profile_the_pack_dropped_are_unreplayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    ids = [rig.shortened()]
    rig.reflect(agents_edit(PREFERENCE, ids))
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    without_chat = loaded.pack.model_copy(
        update={"profiles": {k: v for k, v in loaded.pack.profiles.items() if k != "chat"}}
    )
    monkeypatch.setattr("dot.memory.replay.load_pack", lambda _: loaded.model_copy(update={"pack": without_chat}))

    (version,) = rig.gate()

    assert version.status == "needs_review"
    assert version.detail["gate"]["unreplayable"] == [
        {"episode": ids[0], "reason": "profile 'chat' is no longer in the pack"}
    ]
