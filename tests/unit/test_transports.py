"""The worker's real transports: Tavily search, SMTP email, and their default wiring."""

from __future__ import annotations

import json
import smtplib
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from dot.assembly import default_tool_deps
from dot.config import Settings
from dot.tools.artifacts import ArtifactStore
from dot.tools.native.deps import ToolDeps
from dot.tools.native.email import build_send_email
from dot.tools.native.fetch import HttpxFetcher
from dot.tools.native.search import build_web_search
from dot.tools.native.smtp import SmtpTransport
from dot.tools.native.tavily import TAVILY_URL, TavilySearch

KEY = "tvly-test-key-0000"


def _tavily(handler: Any) -> TavilySearch:
    return TavilySearch(KEY, httpx.Client(transport=httpx.MockTransport(handler)))


def test_tavily_sends_the_key_in_a_header_and_maps_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        results = [{"title": "Open Dot", "url": "https://example.com/dot", "content": "A runtime.", "score": 0.9}]
        return httpx.Response(200, json={"results": results})

    [hit] = _tavily(handler).search("open dot", limit=3)
    assert (hit.title, hit.url, hit.snippet) == ("Open Dot", "https://example.com/dot", "A runtime.")
    [request] = seen
    assert str(request.url) == TAVILY_URL
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body == {"query": "open dot", "max_results": 3, "search_depth": "basic"}
    assert KEY not in json.dumps(body)


def test_a_search_failure_is_a_tool_result_without_the_key(tmp_path: Path) -> None:
    search = _tavily(lambda request: httpx.Response(401, json={"detail": f"bad key {KEY}"}))
    tool = build_web_search(ToolDeps(ArtifactStore(tmp_path), search=search))
    result = json.loads(tool.invoke({"query": "anything"}))
    assert result == {"ok": False, "error": "search returned HTTP 401"}

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {KEY}")

    tool = build_web_search(ToolDeps(ArtifactStore(tmp_path), search=_tavily(boom)))
    result = json.loads(tool.invoke({"query": "anything"}))
    assert result["ok"] is False and KEY not in json.dumps(result)


class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]] = []
    fail_login: ClassVar[bool] = False

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port = host, port
        self.calls: list[tuple[str, Any]] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append(("quit", None))

    def starttls(self, context: Any) -> None:
        self.calls.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        if _FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, f"rejected {user} {password}".encode())
        self.calls.append(("login", (user, password)))

    def send_message(self, message: Any) -> None:
        self.calls.append(("send", message))


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSMTP]:
    _FakeSMTP.instances, _FakeSMTP.fail_login = [], False
    monkeypatch.setattr("dot.tools.native.smtp.smtplib.SMTP", _FakeSMTP)
    return _FakeSMTP


class _Broker:
    def resolve(self, handle: str) -> str:
        assert handle == "cred:smtp"
        return "app-password-123"


def test_smtp_uses_starttls_and_the_brokered_password(tmp_path: Path, fake_smtp: type[_FakeSMTP]) -> None:
    transport = SmtpTransport("smtp.example.com", 587, "dot@example.com", "Dot <dot@example.com>")
    tool = build_send_email(ToolDeps(ArtifactStore(tmp_path), email=transport, credentials=_Broker()))
    result = json.loads(tool.invoke({"to": "sam@example.com", "subject": "Brief", "body": "Two runtimes."}))

    assert result["ok"] is True and result["message_id"].startswith("<")
    assert "app-password-123" not in json.dumps(result)
    [server] = fake_smtp.instances
    assert (server.host, server.port) == ("smtp.example.com", 587)
    kinds = [kind for kind, _ in server.calls]
    assert kinds == ["starttls", "login", "send", "quit"]
    assert server.calls[1][1] == ("dot@example.com", "app-password-123")
    message = server.calls[2][1]
    assert (message["To"], message["From"], message["Subject"]) == ("sam@example.com", "Dot <dot@example.com>", "Brief")


def test_an_smtp_failure_does_not_echo_the_password(tmp_path: Path, fake_smtp: type[_FakeSMTP]) -> None:
    fake_smtp.fail_login = True
    transport = SmtpTransport("smtp.example.com", 587, "dot@example.com", "dot@example.com")
    tool = build_send_email(ToolDeps(ArtifactStore(tmp_path), email=transport, credentials=_Broker()))
    result = json.loads(tool.invoke({"to": "sam@example.com", "subject": "Brief", "body": "Hi."}))
    assert result == {"ok": False, "error": "email delivery failed (SMTPAuthenticationError)"}


def test_default_deps_enable_only_configured_transports(tmp_path: Path) -> None:
    bare = default_tool_deps(Settings(_env_file=None, object_root=str(tmp_path)), "dot-1")  # type: ignore[call-arg]
    assert bare.search is None and bare.email is None and isinstance(bare.fetcher, HttpxFetcher)
    full = default_tool_deps(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            object_root=str(tmp_path),
            tavily_api_key=KEY,
            smtp_host="smtp.example.com",
            smtp_username="dot@example.com",
            smtp_sender="dot@example.com",
        ),
        "dot-1",
    )
    assert isinstance(full.search, TavilySearch) and isinstance(full.email, SmtpTransport)
    assert full.artifacts.root == tmp_path / "dot-1"
