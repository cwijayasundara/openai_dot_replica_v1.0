"""In-memory recon workbench served through ``httpx.MockTransport``.

Tests drive the real ``ReconClient`` against it, and assert on ``paths`` that no
request ever reached a gate route.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

import httpx

from dot.tools.native.recon_client import ReconClient

BASE_URL = "http://recon.test"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeRecon:
    def __init__(self, sponsors: list[dict[str, Any]] | None = None) -> None:
        self.sponsors: list[dict[str, Any]] = (
            sponsors if sponsors is not None else [{"id": "sponsor-a", "name": "Sponsor A"}]
        )
        # Records as GET /runs returns them; ``states`` holds the extra GET /runs/{id} fields.
        self.runs: list[dict[str, Any]] = []
        self.states: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.uploads: list[dict[str, Any]] = []

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def client(self) -> ReconClient:
        return ReconClient(BASE_URL, credentials=None, transport=httpx.MockTransport(self.handle))

    def add_run(self, sponsor_id: str, upload_name: str, upload_sha: str, **state: Any) -> str:
        run_id = f"run-{len(self.runs) + 1}"
        now = _now()
        self.runs.append(
            {
                "id": run_id,
                "sponsor_id": sponsor_id,
                "entity": "affiliate",
                "status": state.pop("status", "awaiting_brief"),
                "upload_name": upload_name,
                "upload_sha": upload_sha,
                "created_at": now,
                "updated_at": now,
            }
        )
        self.states[run_id] = {
            "phase": "p1",
            "pending": None,
            "brief": None,
            "error": None,
            "working": False,
            "artifacts": [],
            **state,
        }
        return run_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/sponsors":
            return httpx.Response(200, json=self.sponsors)
        if request.method == "GET" and path == "/runs":
            sponsor_id = request.url.params.get("sponsor_id")
            return httpx.Response(200, json=[r for r in self.runs if sponsor_id in (None, r["sponsor_id"])])
        if request.method == "GET" and path.startswith("/runs/"):
            run_id = path.removeprefix("/runs/")
            record = next((r for r in self.runs if r["id"] == run_id), None)
            if record is None:
                return httpx.Response(404, json={"detail": "run not found"})
            return httpx.Response(200, json={**record, **self.states[run_id]})
        if request.method == "POST" and path == "/runs":
            return self._start(request)
        return httpx.Response(404, json={"detail": "not found"})

    def _start(self, request: httpx.Request) -> httpx.Response:
        head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        message = BytesParser(policy=HTTP).parsebytes(head + request.content)
        fields: dict[str, str] = {}
        file_name, data = "", b""
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if part.get_filename():
                file_name, data = part.get_filename(), payload
            else:
                fields[str(name)] = payload.decode()
        sponsor_id = fields.get("sponsor_id", "")
        if sponsor_id not in {s["id"] for s in self.sponsors} or fields.get("entity") != "affiliate":
            return httpx.Response(422, json={"detail": "invalid sponsor or entity"})
        self.uploads.append({"sponsor_id": sponsor_id, "file_name": file_name, "data": data})
        run_id = self.add_run(sponsor_id, file_name, hashlib.sha256(data).hexdigest())
        return httpx.Response(202, json={"run_id": run_id})
