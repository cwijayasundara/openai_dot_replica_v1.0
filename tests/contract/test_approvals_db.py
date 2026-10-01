"""Approval persistence and paused inbox scheduling against local PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from dot.assembly import build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import Repositories
from dot.runtime.router import enqueue
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import Worker, run_agent_turn
from dot.surfaces.api import create_app
from dot.surfaces.dots import create_dot
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps
from tests.contract import test_inbox_worker as db_fixtures
from tests.support.scripted_model import ScriptedChatModel, call, say, tools
from tests.unit.test_policy_assembly import Credentials, Email

pytestmark = pytest.mark.db
pool = db_fixtures.pool
repos = db_fixtures.repos


@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
def test_worker_resume_and_atomic_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pool: ConnectionPool,
    repos: Repositories,
    decision: str,
) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = ["reviewer"]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)
    events = InMemoryEventChannel()
    email = Email()
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), email=email, credentials=Credentials())
    model = ScriptedChatModel(
        script=[
            tools(call("send_email", to="original@example.com", subject="hi", body="hello")),
            say("done"),
            say("next turn"),
        ]
    )
    app = create_app(settings, repos=repos, runtime=runtime, events=events, model=model)
    principal = ["outsider"]

    @app.middleware("http")
    async def trusted_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.user_id = principal[0]
        return await call_next(request)

    worker = Worker(
        pool,
        repos,
        events,
        lambda dot, profile, batch, channel: run_agent_turn(
            dot,
            profile,
            batch,
            channel,
            settings=settings,
            runtime=runtime,
            model=model,
            deps=deps,
        ),
    )
    try:
        dot = create_dot(repos, runtime, "research-analyst", "owner")
        enqueue(repos, dot.dot_id, "web", {"text": "Send an email"}, "chat")
        assert worker.run_once()
        assert repos.get_dot(dot.dot_id).status == "paused"
        approval_id = next(e.detail["approval_id"] for e in events.events if e.kind == "approval")
        assert repos.get_approval(approval_id).status == "pending"
        next_message = enqueue(repos, dot.dot_id, "web", {"text": "Later message"}, "digest")
        assert not worker.run_once()
        with TestClient(app) as client:
            endpoint = f"/approvals/{approval_id}"
            assert client.post(endpoint, json={"type": "approve"}).status_code == 403
            assert not worker.run_once() and not email.sent
            principal[0] = "reviewer"
            body = {"type": decision}
            if decision == "edit":
                body["edited_args"] = {"to": "edited@example.com", "subject": "hi", "body": "edited"}  # type: ignore[assignment]
            with monkeypatch.context() as failure:

                def fail_audit(conn, event):
                    raise RuntimeError("audit unavailable")

                failure.setattr("dot.persistence.db._insert_audit", fail_audit)
                with pytest.raises(RuntimeError, match="audit unavailable"):
                    client.post(endpoint, json=body)
            assert repos.get_approval(approval_id).status == "pending"
            with pool.connection() as conn:
                assert conn.execute("SELECT count(*) FROM episodes WHERE dot_id=%s", (dot.dot_id,)).fetchone() == (0,)
                assert conn.execute(
                    "SELECT count(*) FROM inbox WHERE dot_id=%s AND source='approval'", (dot.dot_id,)
                ).fetchone() == (0,)
            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(lambda _: client.post(endpoint, json=body).status_code, range(2)))
            assert sorted(statuses) == [202, 409]
        review_audit = [event for event in repos.list_audit(dot.dot_id) if event.kind == "approval"]
        assert [event.decision for event in review_audit] == ["pending", decision]
        assert review_audit[1].actor == "reviewer"
        assert review_audit[0].detail["turn_id"] == review_audit[1].detail["turn_id"]
        with pool.connection() as conn:
            assert conn.execute("SELECT count(*) FROM episodes WHERE dot_id=%s", (dot.dot_id,)).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM inbox WHERE dot_id=%s AND source='approval'", (dot.dot_id,)
            ).fetchone() == (1,)
        assert worker.run_once()  # resume skips the earlier ordinary inbox message
        assert repos.get_inbox(next_message.id).claimed_at is None
        assert email.sent == (
            [] if decision == "reject" else ["edited@example.com" if decision == "edit" else "original@example.com"]
        )
        assert repos.get_dot(dot.dot_id).status == "active"
        assert worker.run_once()
        assert repos.get_inbox(next_message.id).done_at is not None
        assert not worker.run_once()
    finally:
        runtime.close()
