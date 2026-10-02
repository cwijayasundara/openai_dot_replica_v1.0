"""Onboarding-ops acceptance with a scripted model: a dropped file to a drafted sponsor email.

Sweeps record findings, the intake digest proposes a run that a human approves,
the status sweep sees the run wait at the brief gate, and the daily digest drafts
the sponsor email. Nothing ever reaches a workbench gate route.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from dot.assembly import build_graph_runtime
from dot.channels.base import DeliveringEventChannel
from dot.channels.outbox import MemoryOutbox
from dot.config import Settings
from dot.persistence.db import ChannelBinding, Finding, InboxMessage, MemoryRepositories
from dot.proactive.findings import OPEN, REPORTED
from dot.proactive.scheduler import trigger
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.safety.approvals import ReviewDecision, decide
from dot.surfaces.dots import create_dot
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps
from tests.support.fake_recon import FakeRecon
from tests.support.scripted_model import ScriptedChatModel, call, say, tools
from tests.unit.test_proactive import _Refuse

AFFILIATES = b"affiliate_id,name,country\nA1,Acme Ltd,GB\nA2,Beta GmbH,DE\nA3,Gamma SA,FR\n"
SHA = hashlib.sha256(AFFILIATES).hexdigest()


def _results(model: ScriptedChatModel) -> dict[str, dict[str, Any]]:
    """The last tool result the model saw for each tool name."""
    seen = [m for messages in model.seen for m in messages if isinstance(m, ToolMessage)]
    return {str(m.name): json.loads(str(m.content)) for m in seen}


def test_a_dropped_file_becomes_an_approved_run_and_a_drafted_sponsor_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dot.safety.approvals.approvers", lambda _: frozenset({"reviewer"}))
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    repos = MemoryRepositories()
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    try:
        dot = create_dot(repos, runtime, "onboarding-ops", "owner")
        repos.bind_channel(ChannelBinding(dot.dot_id, "slack", "C123"))
        outbox = MemoryOutbox(repos)
        events = DeliveringEventChannel(InMemoryEventChannel(), outbox, ["slack"])
        fake = FakeRecon()
        drops = tmp_path / "drops"
        refuse = _Refuse()
        # recon_declined is left unset so assembly wires it to the dot's rejected start_run cards.
        deps = ToolDeps(
            ArtifactStore(tmp_path / "artifacts"), email=refuse, slack=refuse, recon=fake.client(), drop_root=drops
        )
        (drops / "sponsor-a").mkdir(parents=True)
        (drops / "sponsor-a" / "affiliates.csv").write_bytes(AFFILIATES)

        def run(message: InboxMessage, model: ScriptedChatModel) -> None:
            run_agent_turn(
                dot, message.profile, [message], events, settings=settings, runtime=runtime, model=model, deps=deps
            )
            # As the worker does once the turn returns.
            now = datetime.now(UTC)
            repos.update_inbox(replace(message, claimed_at=now, done_at=now))

        def fire(name: str, model: ScriptedChatModel, at: datetime) -> None:
            message = trigger(repos, dot, name, at)
            assert message is not None
            run(message, model)

        def posted() -> list[tuple[str, str]]:
            """Drain the Slack outbox as (kind, text) pairs, as the delivery loop would."""
            items = []
            while (item := outbox.next("slack")) is not None:
                assert item.target == {"channel": "C123"}
                items.append((item.kind, str(item.body.get("text", ""))))
                outbox.delivered(item.id)
            return items

        def findings(schedule: str) -> list[Finding]:
            return [f for f in repos.list_findings(dot.dot_id) if f.schedule == schedule]

        # 1. The intake sweep sees the new file and records it.
        sweep = ScriptedChatModel(
            script=[
                tools(call("list_drops")),
                tools(
                    call(
                        "record_finding",
                        title="New file affiliates.csv for sponsor-a",
                        summary=f"sponsor_id sponsor-a, file_name affiliates.csv, sha256 {SHA}",
                        score=0.9,
                    )
                ),
                say("Recorded one new file."),
            ]
        )
        fire("intake-sweep", sweep, datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
        [drop] = _results(sweep)["list_drops"]["files"]
        assert (drop["sponsor_id"], drop["file_name"], drop["sha256"]) == ("sponsor-a", "affiliates.csv", SHA)
        assert drop["supported"] and drop["run_id"] is None and not drop["declined"]
        [intake_finding] = findings("intake-sweep")
        assert intake_finding.status == OPEN

        # 2. The intake digest proposes start_run; the turn waits at an approval card.
        intake = ScriptedChatModel(
            script=[
                tools(call("start_run", sponsor_id="sponsor-a", file_name="affiliates.csv", sha256=SHA)),
                say("Started a run for affiliates.csv (sponsor-a)."),
            ]
        )
        fire("intake", intake, datetime(2026, 10, 5, 9, 5, tzinfo=UTC))
        [card] = repos.list_dot_approvals(dot.dot_id, "pending")
        assert card.tool == "start_run"
        assert card.args == {"sponsor_id": "sponsor-a", "file_name": "affiliates.csv", "sha256": SHA}
        assert fake.uploads == [] and "/runs" not in [r.url.path for r in fake.requests if r.method == "POST"]
        [intake_finding] = findings("intake-sweep")
        assert intake_finding.status == REPORTED
        assert [kind for kind, _ in posted()] == ["approval"]

        # 3. A human approves; the resumed turn uploads the file's bytes.
        decide(repos, card.approval_id, "reviewer", ReviewDecision(type="approve"))
        resume = next(m for m in repos.inbox.values() if m.source == "approval" and m.done_at is None)
        run(resume, intake)
        [post] = [r for r in fake.requests if r.method == "POST"]
        assert post.url.path == "/runs"
        assert fake.uploads == [{"sponsor_id": "sponsor-a", "file_name": "affiliates.csv", "data": AFFILIATES}]
        started = _results(intake)["start_run"]
        assert started["ok"] is True
        run_id = started["run_id"]
        assert run_id == fake.runs[0]["id"]
        assert posted() == [("message", "Started a run for affiliates.csv (sponsor-a).")]

        # The workbench moves the run to the brief gate with one question.
        fake.states[run_id].update(
            pending={"gate": "brief", "message": "Answer the brief questions", "blocked_reasons": []},
            brief={"questions": [{"id": "q1", "text": "Which country is the home market?", "options": None}]},
        )

        # 4. The status sweep reads the run and records that it waits at the brief gate.
        status = ScriptedChatModel(
            script=[
                tools(call("list_runs")),
                tools(call("get_run", run_id=run_id)),
                tools(
                    call(
                        "record_finding",
                        title=f"Run {run_id} waiting at brief",
                        summary="phase p1, awaiting_brief; question: Which country is the home market?",
                        score=0.8,
                    )
                ),
                say("One run waits at the brief gate."),
            ]
        )
        fire("status-sweep", status, datetime(2026, 10, 5, 10, 0, tzinfo=UTC))
        sweep_results = _results(status)
        assert [r["run_id"] for r in sweep_results["list_runs"]["runs"]] == [run_id]
        assert sweep_results["get_run"]["gate"] == "brief"
        assert [q["text"] for q in sweep_results["get_run"]["brief_questions"]] == ["Which country is the home market?"]
        [status_finding] = findings("status-sweep")
        assert status_finding.status == OPEN and status_finding.title == f"Run {run_id} waiting at brief"

        # 5. The daily digest drafts the sponsor email without an approval and replies.
        daily = ScriptedChatModel(
            script=[
                tools(call("get_run", run_id=run_id)),
                tools(
                    call(
                        "draft_email",
                        to="ops@sponsor-a.example",
                        subject=f"Brief questions for {run_id}",
                        body="Which country is the home market?",
                    )
                ),
                say(f"Onboarding digest: {run_id} (sponsor-a) waits at the brief gate; a sponsor email is drafted."),
            ]
        )
        fire("daily", daily, datetime(2026, 10, 6, 8, 45, tzinfo=UTC))
        draft = _results(daily)["draft_email"]
        assert draft["ok"] is True and draft["to"] == "ops@sponsor-a.example" and draft["artifact_id"]
        assert repos.list_dot_approvals(dot.dot_id, "pending") == []
        assert [c.tool for c in repos.approvals.values()] == ["start_run"]
        [(kind, reply)] = posted()
        assert kind == "message" and reply.startswith(f"Onboarding digest: {run_id}")
        [status_finding] = findings("status-sweep")
        assert status_finding.status == REPORTED
        # The daily snapshot held only the status sweep's finding. The digest shares the dot's
        # thread with intake, so its request is the last human message, not the first.
        request = [m for m in daily.seen[0] if isinstance(m, HumanMessage)][-1]
        assert str(request.content).startswith("[schedule] Send the morning onboarding digest")
        text = str(request.content)
        assert f'"finding_id":{status_finding.id}' in text
        assert f'"finding_id":{intake_finding.id}' not in text and "affiliates.csv" not in text

        # 6. No request ever reached a gate route.
        assert not [path for path in fake.paths if "/gate" in path]
    finally:
        runtime.close()
