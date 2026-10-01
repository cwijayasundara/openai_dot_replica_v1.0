"""Read routes behind the web UI: owner or approver only, compact and redacted."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dot.assembly import GraphRuntime, build_graph_runtime
from dot.config import Settings
from dot.persistence.db import Approval, AuditEvent, Dot, Finding, MemoryRepositories, MemoryVersion
from dot.runtime.router import enqueue
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.surfaces.api import create_app
from dot.surfaces.dots import create_dot
from tests.support.repository_contract import WHEN
from tests.support.scripted_model import ScriptedChatModel, say

SECRET = "sk-live-0123456789abcdef0123456789"


@dataclass
class Rig:
    client: TestClient
    repos: MemoryRepositories
    runtime: GraphRuntime
    settings: Settings
    model: ScriptedChatModel
    dot: Dot


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    settings = Settings(
        _env_file=None, database_url=None, object_root=str(tmp_path), web_auth="dev", web_dev_user="ada"
    )  # type: ignore[call-arg]
    repos = MemoryRepositories()
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    runtime.redactor.add(SECRET)
    model = ScriptedChatModel(script=[say("Hello from the dot.")])
    monkeypatch.setattr("dot.surfaces.api.approvers", lambda pack: frozenset({"reviewer"}))
    app = create_app(settings, repos=repos, runtime=runtime, events=InMemoryEventChannel(), model=model)
    dot = create_dot(repos, runtime, "research-analyst", "ada")
    with TestClient(app) as client:
        yield Rig(client, repos, runtime, settings, model, dot)


def test_me_packs_and_my_dots(rig: Rig) -> None:
    assert rig.client.get("/me").json() == {"user_id": "ada"}
    [pack] = rig.client.get("/packs").json()["packs"]
    assert pack["name"] == "research-analyst" and "chat" in pack["profiles"]
    assert [d["dot_id"] for d in rig.client.get("/dots").json()["dots"]] == [rig.dot.dot_id]


def test_reads_need_the_owner_or_an_approver(tmp_path: Path, rig: Rig) -> None:
    routes = ["jobs", "approvals", "findings", "memory", "sandbox", "audit"]
    for route in routes:
        assert rig.client.get(f"/dots/{rig.dot.dot_id}/{route}").status_code == 200
        assert rig.client.get(f"/dots/missing/{route}").status_code == 404

    stranger = create_dot(rig.repos, rig.runtime, "research-analyst", "someone-else")
    for route in routes:
        assert rig.client.get(f"/dots/{stranger.dot_id}/{route}").status_code == 403

    anonymous = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))  # type: ignore[call-arg]
    app = create_app(anonymous, repos=rig.repos, runtime=rig.runtime, events=InMemoryEventChannel())
    with TestClient(app) as client:
        assert client.get("/me").status_code == 401
        assert client.get("/dots").status_code == 401
        for route in routes:
            assert client.get(f"/dots/{rig.dot.dot_id}/{route}").status_code == 401


def test_jobs_approvals_findings_and_memory_are_listed_redacted(rig: Rig) -> None:
    dot_id = rig.dot.dot_id
    store_job = rig.client.app.state.surface.jobs.create(  # type: ignore[attr-defined]
        dot_id, "researcher", f"Find runtimes with {SECRET}", profile="chat", origin={}
    )
    run_ref = json.dumps({"thread_id": dot_id, "profile": "chat", "task": "t"})
    rig.repos.create_approval(Approval("a-old", dot_id, run_ref, "send_email", {"to": "x@example.com"}, "approve"))
    rig.repos.create_approval(
        Approval("a-new", dot_id, run_ref, "send_email", {"to": "y@example.com", "body": SECRET}, "pending")
    )
    rig.repos.insert_finding(
        Finding(0, dot_id, "sweep", "New filing", {"url": "https://example.com"}, 0.8, "open", WHEN)
    )
    rig.repos.insert_memory_version(MemoryVersion(0, dot_id, WHEN, "-long\n+short", [1, 2], "accepted"))

    [job] = rig.client.get(f"/dots/{dot_id}/jobs").json()["jobs"]
    assert job["job_id"] == store_job.job_id and job["status"] == "queued"
    assert SECRET not in job["instructions"]

    cards = rig.client.get(f"/dots/{dot_id}/approvals").json()["approvals"]
    assert [card["approval_id"] for card in cards] == ["a-new", "a-old"]
    assert cards[0]["allowed_decisions"] == ["approve", "edit", "reject"] and cards[1]["allowed_decisions"] == []
    assert SECRET not in json.dumps(cards)
    pending = rig.client.get(f"/dots/{dot_id}/approvals", params={"status": "pending"}).json()["approvals"]
    assert [card["approval_id"] for card in pending] == ["a-new"]

    [finding] = rig.client.get(f"/dots/{dot_id}/findings").json()["findings"]
    assert finding["title"] == "New filing" and finding["status"] == "open"
    [version] = rig.client.get(f"/dots/{dot_id}/memory").json()["versions"]
    assert version["diff"] == "-long\n+short" and version["episodes"] == [1, 2]


def test_sandbox_activity_is_the_sandboxed_subagents_audit(rig: Rig) -> None:
    dot_id = rig.dot.dot_id
    rig.repos.append_audit(AuditEvent(0, dot_id, WHEN, "supervisor", "tool_call", tool="web_search"))
    coder = rig.repos.append_audit(AuditEvent(0, dot_id, WHEN, "coder", "tool_call", tool="execute"))
    body = rig.client.get(f"/dots/{dot_id}/sandbox").json()
    assert body["actors"] == ["coder"]
    assert [event["id"] for event in body["events"]] == [coder.id]


def test_the_thread_names_each_message_source(rig: Rig) -> None:
    slack = enqueue(rig.repos, rig.dot.dot_id, "slack", {"text": "hi from Slack"}, "chat")
    events = InMemoryEventChannel()
    run_agent_turn(rig.dot, "chat", [slack], events, settings=rig.settings, runtime=rig.runtime, model=rig.model)
    messages = rig.client.get(f"/dots/{rig.dot.dot_id}/thread").json()["messages"]
    assert [(m["role"], m.get("source")) for m in messages] == [("human", "slack"), ("ai", None)]
