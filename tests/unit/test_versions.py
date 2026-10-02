"""L4: approvers roll back accepted edits and settle held ones; every action is audited and refused on conflict."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from dot.assembly import build_graph_runtime
from dot.config import Settings
from dot.memory.reflection import AGENTS_PATH, MemoryFiles
from dot.persistence.db import Dot, MemoryRepositories, MemoryVersion
from dot.surfaces.api import create_app
from dot.surfaces.dots import create_dot
from tests.support.scripted_model import ScriptedChatModel

SKILL = "/memories/skills/email-drafting/SKILL.md"


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dot.surfaces.api.approvers", lambda _: ["reviewer"])
        settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))  # type: ignore[call-arg]
        self.repos = MemoryRepositories()
        runtime = build_graph_runtime(settings)
        self.dot: Dot = create_dot(self.repos, runtime, "research-analyst", "owner")
        self.files = MemoryFiles(runtime.store, self.dot.dot_id)
        self.app = app = create_app(settings, repos=self.repos, runtime=runtime, model=ScriptedChatModel(script=[]))
        self.user: list[str | None] = ["reviewer"]

        @app.middleware("http")
        async def trusted_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
            request.state.user_id = self.user[0]
            return await call_next(request)

        self.client = TestClient(app)

    def held(self, path: str, find: str, replace: str) -> MemoryVersion:
        detail = {"path": path, "find": find, "replace": replace, "rationale": "Shorter emails."}
        return self.repos.insert_memory_version(
            MemoryVersion(0, self.dot.dot_id, datetime.now(UTC), "", [], "needs_review", detail)
        )

    def act(self, version: MemoryVersion, action: str) -> Any:
        return self.client.post(f"/dots/{self.dot.dot_id}/memory/{version.id}/{action}")

    def memory_events(self) -> list[tuple[str, str | None]]:
        return [(e.actor, e.decision) for e in self.repos.list_audit(self.dot.dot_id) if e.kind == "memory"]

    def status(self, version: MemoryVersion) -> str:
        return self.repos.get_memory_version(version.id).status


def test_the_memory_list_says_whether_the_viewer_can_act(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    assert rig.client.get(f"/dots/{rig.dot.dot_id}/memory").json()["can_act"] is True
    rig.user[0] = "owner"  # the owner may view but is not an approver
    assert rig.client.get(f"/dots/{rig.dot.dot_id}/memory").json()["can_act"] is False


def test_accept_then_roll_back_restores_the_skill_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    original = rig.files.read(SKILL)
    assert original is not None
    version = rig.held(SKILL, "- Keep it short.", "- Keep it under 60 words.")

    accepted = rig.act(version, "accept")
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["status"] == "accepted" and body["detail"]["accept"]["by"] == "reviewer"
    assert "before" not in body["detail"]  # kept for rollback, not shown
    assert "-- Keep it short." in body["diff"]
    assert rig.files.read(SKILL) == original.replace("- Keep it short.", "- Keep it under 60 words.")

    rolled = rig.act(version, "rollback")
    assert rolled.status_code == 200 and rolled.json()["status"] == "rolled_back"
    assert rig.files.read(SKILL) == original
    assert rig.memory_events() == [("reviewer", "accept"), ("reviewer", "rollback")]
    # Rolled back already: a second rollback is a conflict and changes nothing.
    assert rig.act(version, "rollback").status_code == 409
    assert rig.memory_events() == [("reviewer", "accept"), ("reviewer", "rollback")]


def test_rolling_back_a_created_file_removes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    version = rig.held(AGENTS_PATH, "", "- Keep emails short.\n")
    assert rig.act(version, "accept").status_code == 200
    assert rig.files.read(AGENTS_PATH) == "- Keep emails short.\n"
    assert rig.act(version, "rollback").status_code == 200
    assert rig.files.read(AGENTS_PATH) is None


def test_rollback_refuses_when_a_later_edit_is_built_on_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    first = rig.held(AGENTS_PATH, "", "- Keep emails short.\n")
    assert rig.act(first, "accept").status_code == 200
    # The later edit's find is the first edit's whole text: it is built on it.
    second = rig.held(AGENTS_PATH, "- Keep emails short.\n", "- Keep emails short.\n- Cite sources.\n")
    assert rig.act(second, "accept").status_code == 200

    refused = rig.act(first, "rollback")
    assert refused.status_code == 409 and "built on this one" in refused.json()["detail"]
    assert rig.files.read(AGENTS_PATH) == "- Keep emails short.\n- Cite sources.\n"
    # Newest first: each is restored exactly, and the file the first one created is gone.
    assert rig.act(second, "rollback").status_code == 200
    assert rig.files.read(AGENTS_PATH) == "- Keep emails short.\n"
    assert rig.act(first, "rollback").status_code == 200
    assert rig.files.read(AGENTS_PATH) is None


def test_an_edit_already_in_the_file_is_not_applied_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    original = rig.files.read(SKILL) or ""
    # The edit keeps its find text: without the guard it would apply again.
    appended = "- Keep it short. Never attach files."
    rig.files.write(SKILL, original.replace("- Keep it short.", appended))
    # As if a crash had written the file but not the row.
    version = rig.held(SKILL, "- Keep it short.", appended)
    refused = rig.act(version, "accept")
    assert refused.status_code == 409 and "already in the file" in refused.json()["detail"]
    assert (rig.files.read(SKILL) or "").count("Never attach files.") == 1


def test_rollback_refuses_when_a_later_edit_rewrote_its_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    first = rig.held(SKILL, "- Keep it short.", "- Keep it under 60 words.")
    assert rig.act(first, "accept").status_code == 200
    later = rig.held(SKILL, "- Keep it under 60 words.", "- Keep it under 40 words.")
    assert rig.act(later, "accept").status_code == 200
    current = rig.files.read(SKILL)

    refused = rig.act(first, "rollback")

    assert refused.status_code == 409
    assert "later edit" in refused.json()["detail"]
    assert rig.files.read(SKILL) == current
    assert rig.status(first) == "accepted"
    # The later edit's own rollback still works, then the first one's does.
    assert rig.act(later, "rollback").status_code == 200
    assert rig.act(first, "rollback").status_code == 200
    assert "- Keep it short." in (rig.files.read(SKILL) or "")


def test_a_rollback_with_other_text_added_since_swaps_its_text_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    original = rig.files.read(SKILL) or ""
    first = rig.held(SKILL, "- Keep it short.", "- Keep it under 60 words.")
    assert rig.act(first, "accept").status_code == 200
    other = rig.held(SKILL, "# Email drafting", "# Email drafting (house style)")
    assert rig.act(other, "accept").status_code == 200

    assert rig.act(first, "rollback").status_code == 200
    assert rig.files.read(SKILL) == original.replace("# Email drafting", "# Email drafting (house style)")


def test_discard_and_stale_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    before = rig.files.listing()
    dropped = rig.held(AGENTS_PATH, "", "- Something.\n")
    assert rig.act(dropped, "discard").json()["status"] == "discarded"
    assert rig.files.listing() == before
    assert rig.act(dropped, "accept").status_code == 409

    stale = rig.held(SKILL, "text that is not in the skill", "x")
    refused = rig.act(stale, "accept")
    assert refused.status_code == 409 and "no longer applies" in refused.json()["detail"]
    assert rig.status(stale) == "needs_review"
    assert rig.files.listing() == before
    assert rig.memory_events() == [("reviewer", "discard")]


def test_only_approvers_may_act_on_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    version = rig.held(AGENTS_PATH, "", "- Keep emails short.\n")
    rig.user[0] = None
    assert rig.act(version, "accept").status_code == 401
    rig.user[0] = "owner"  # the owner is not a pack approver here
    assert rig.act(version, "accept").status_code == 403
    rig.user[0] = "reviewer"
    assert rig.client.post(f"/dots/{rig.dot.dot_id}/memory/{version.id}/apply").status_code == 422
    other = create_dot(
        rig.repos, build_graph_runtime(Settings(_env_file=None, database_url=None)), "research-analyst", "owner"
    )  # type: ignore[call-arg]
    assert rig.client.post(f"/dots/{other.dot_id}/memory/{version.id}/accept").status_code == 404
    assert rig.status(version) == "needs_review" and rig.memory_events() == []


def test_a_memory_action_streams_a_memory_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    version = rig.held(AGENTS_PATH, "", "- Keep emails short.\n")
    assert rig.act(version, "discard").status_code == 200
    events = [e for e in rig.app.state.surface.events.events if e.kind == "memory"]
    assert [(e.dot_id, e.detail) for e in events] == [(rig.dot.dot_id, {"version": version.id, "status": "discarded"})]
    assert rig.act(version, "discard").status_code == 409
    assert len([e for e in rig.app.state.surface.events.events if e.kind == "memory"]) == 1  # no event on a refusal
