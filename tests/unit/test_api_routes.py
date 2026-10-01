"""API routes with in-memory repositories. The scripted turn is the database contract test."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dot.assembly import build_graph_runtime
from dot.config import Settings
from dot.persistence.db import open_repositories
from dot.runtime.turns import InMemoryEventChannel
from dot.surfaces.api import create_app
from tests.support.scripted_model import ScriptedChatModel


def _client(tmp_path: Path) -> tuple[TestClient, InMemoryEventChannel, ScriptedChatModel]:
    settings = Settings(_env_file=None, object_root=str(tmp_path / "objects"), database_url=None)  # type: ignore[call-arg]
    events = InMemoryEventChannel()
    model = ScriptedChatModel(script=[])
    app = create_app(
        settings,
        repos=open_repositories(None),
        runtime=build_graph_runtime(settings),
        events=events,
        model=model,
    )
    return TestClient(app), events, model


def test_create_get_and_queue_a_message(tmp_path: Path) -> None:
    client, events, model = _client(tmp_path)
    with client:
        created = client.post("/dots", json={"pack": "research-analyst", "owner_user_id": "ada", "display_name": "Ada"})
        assert created.status_code == 201
        body = created.json()
        assert body["pack_name"] == "research-analyst"
        assert body["thread_id"] == body["dot_id"]
        assert body["status"] == "active"
        dot_id = body["dot_id"]

        assert client.get(f"/dots/{dot_id}").json()["owner_user_id"] == "ada"
        queued = client.post(f"/dots/{dot_id}/messages", json={"text": "hello"})
        assert queued.status_code == 202
        assert queued.json()["profile"] == "chat"
        thread = client.get(f"/dots/{dot_id}/thread")
        assert thread.status_code == 200
        assert thread.json()["messages"] == []
        assert model.calls == 0
        assert events.events == []

        missing = client.get("/dots/missing")
        assert missing.status_code == 404
        unknown = client.post("/dots", json={"pack": "no-such-pack", "owner_user_id": "ada"})
        assert unknown.status_code == 400
        bad_profile = client.post(f"/dots/{dot_id}/messages", json={"text": "sweep this", "profile": "nope"})
        assert bad_profile.status_code == 400
        assert "nope" in bad_profile.json()["detail"]
        assert client.get("/health").json() == {"status": "ok"}
