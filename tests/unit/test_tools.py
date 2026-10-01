from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dot.packs.loader import REPO_ROOT, load_pack
from dot.tools.artifacts import ArtifactStore
from dot.tools.effects import Effect
from dot.tools.native import NATIVE_TOOL_NAMES, native_registry
from dot.tools.native.deps import CredentialError, Hit, ToolDeps
from dot.tools.native.envelope import UNTRUSTED_MARKER
from dot.tools.native.fetch import HttpxFetcher, public_http_url
from dot.tools.registry import NATIVE_EFFECTS, ToolRegistry, builtin_registry

SECRET = "smtp-secret-value"
SENTINEL = "SENTINEL_PAST_THE_EXCERPT"


class _Search:
    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        return [Hit("Result", "https://example.test/a", f"snippet for {query}")][:limit]


class _Fetcher:
    def __init__(self, body: str) -> None:
        self.body = body

    def fetch(self, url: str) -> object:
        from dot.tools.native.deps import FetchedPage

        return FetchedPage(url=url, status=200, content_type="text/html", body=self.body)


class _Broker:
    def resolve(self, handle: str) -> str:
        if handle != "cred:smtp":
            raise CredentialError(handle)
        return SECRET


class _BrokenBroker:
    def resolve(self, handle: str) -> str:
        raise CredentialError(handle)


class _Email:
    def __init__(self) -> None:
        self.credential = ""
        self.sent = False

    def send(self, *, to: str, subject: str, body: str, credential: str) -> str:
        self.sent = True
        self.credential = credential
        return "msg-1"


class _Slack:
    def post(self, *, channel: str, text: str) -> str:
        return "123.456"


def _deps(tmp_path: Path, **overrides: object) -> ToolDeps:
    deps = ToolDeps(artifacts=ArtifactStore(tmp_path / "artifacts"))
    for name, value in overrides.items():
        setattr(deps, name, value)
    return deps


def _invoke(registry: ToolRegistry, name: str, payload: dict[str, object]) -> dict[str, object]:
    tool = next(tool for tool in registry.for_profile(_All()) if tool.name == name)
    raw = tool.invoke(payload)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


class _All:
    tools: str | None = "*"
    effects: list[Effect] | None = None


def test_every_native_tool_has_an_effect() -> None:
    registry = builtin_registry()
    assert set(NATIVE_TOOL_NAMES) <= set(NATIVE_EFFECTS)
    for name in NATIVE_TOOL_NAMES:
        assert registry.effect(name) is NATIVE_EFFECTS[name]
        assert registry.effect(name) is not None
    assert registry.effect("execute") is Effect.write


def test_for_profile_filters_by_name_and_effect(tmp_path: Path) -> None:
    registry = native_registry(_deps(tmp_path))
    pack = load_pack(REPO_ROOT / "packs" / "research-analyst")

    chat = [tool.name for tool in registry.for_profile(pack.pack.profiles["chat"])]
    sweep = [tool.name for tool in registry.for_profile(pack.pack.profiles["sweep"])]
    digest = [tool.name for tool in registry.for_profile(pack.pack.profiles["digest"])]

    assert chat == sorted(NATIVE_TOOL_NAMES)
    assert "execute" not in chat
    assert sweep == ["fetch_url", "web_search"]
    assert digest == ["draft_email", "fetch_url", "web_search", "write_report"]
    assert "send_email" not in digest
    assert "slack_post" not in sweep


def test_untagged_tool_is_not_offered() -> None:
    registry = ToolRegistry()
    registry.register("mystery", None)
    offered = registry.for_profile(_All())
    assert offered == []


def test_fetch_wraps_the_page_in_an_untrusted_envelope(tmp_path: Path) -> None:
    body = ("paragraph " * 800) + SENTINEL
    registry = native_registry(_deps(tmp_path, fetcher=_Fetcher(body)))
    result = _invoke(registry, "fetch_url", {"url": "https://example.test/report"})
    encoded = json.dumps(result)
    content = result["content"]
    assert isinstance(content, dict)
    assert content["marker"] == UNTRUSTED_MARKER
    assert content["source"] == "https://example.test/report"
    assert content["truncated"] is True
    assert SENTINEL not in encoded
    artifact_id = result["artifact_id"]
    assert isinstance(artifact_id, str)
    assert SENTINEL in ArtifactStore(tmp_path / "artifacts").read_text(artifact_id)


def test_search_snippets_are_enveloped(tmp_path: Path) -> None:
    registry = native_registry(_deps(tmp_path, search=_Search()))
    result = _invoke(registry, "web_search", {"query": "quarterly filings"})
    results = result["results"]
    assert isinstance(results, list)
    snippet = results[0]["snippet"]
    assert snippet["marker"] == UNTRUSTED_MARKER
    assert "quarterly filings" in snippet["text"]


def test_draft_and_report_return_ids_not_bodies(tmp_path: Path) -> None:
    registry = native_registry(_deps(tmp_path))
    report = _invoke(registry, "write_report", {"title": "Brief", "markdown": "The finding."})
    draft = _invoke(registry, "draft_email", {"to": "sam@example.test", "subject": "Hello", "body": "Please review."})
    assert "The finding." not in json.dumps(report)
    assert "Please review." not in json.dumps(draft)
    store = ArtifactStore(tmp_path / "artifacts")
    assert "The finding." in store.read_text(str(report["artifact_id"]))
    assert "Please review." in store.read_text(str(draft["artifact_id"]))


def test_send_email_hides_the_credential_and_draft_does_not_send(tmp_path: Path) -> None:
    email = _Email()
    registry = native_registry(_deps(tmp_path, email=email, credentials=_Broker()))
    draft_registry = native_registry(_deps(tmp_path, email=email, credentials=_Broker()))
    _invoke(draft_registry, "draft_email", {"to": "sam@example.test", "subject": "Hello", "body": "Draft only."})
    assert email.sent is False

    result = _invoke(
        registry,
        "send_email",
        {"to": "sam@example.test", "subject": "Hello", "body": "The note."},
    )
    assert result["ok"] is True
    assert result["message_id"] == "msg-1"
    assert SECRET not in json.dumps(result)
    assert email.credential == SECRET
    assert "The note." not in json.dumps(result)


def test_send_email_fails_closed_without_a_transport(tmp_path: Path) -> None:
    registry = native_registry(_deps(tmp_path))
    result = _invoke(registry, "send_email", {"to": "sam@example.test", "subject": "Hi", "body": "No."})
    assert result["ok"] is False
    assert "not configured" in str(result["error"])


def test_send_email_hides_broker_failures(tmp_path: Path) -> None:
    registry = native_registry(_deps(tmp_path, email=_Email(), credentials=_BrokenBroker()))
    result = _invoke(registry, "send_email", {"to": "sam@example.test", "subject": "Hi", "body": "No."})
    assert result == {"ok": False, "error": "could not resolve email credentials"}


def test_slack_post_returns_the_timestamp(tmp_path: Path) -> None:
    registry = native_registry(_deps(tmp_path, slack=_Slack()))
    result = _invoke(registry, "slack_post", {"channel": "C1", "text": "Morning."})
    assert result["ok"] is True
    assert result["ts"] == "123.456"
    assert "Morning." not in json.dumps(result)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "http://127.0.0.1/latest", "http://169.254.169.254/", "https://user:pw@example.test/"],
)
def test_fetch_rejects_non_public_urls(url: str) -> None:
    with pytest.raises(ValueError):
        public_http_url(url)


def _public(host: str) -> list[str]:
    return ["93.184.215.14"]


def test_public_url_is_accepted() -> None:
    assert public_http_url("https://example.test/a", _public) == "https://example.test/a"


@pytest.mark.parametrize(
    "addresses",
    [["169.254.169.254"], ["10.0.0.5"], ["93.184.215.14", "127.0.0.1"], ["::1"], []],
)
def test_a_hostname_resolving_to_a_private_address_is_refused(addresses: list[str]) -> None:
    with pytest.raises(ValueError, match="not fetchable"):
        public_http_url("http://metadata.google.internal/computeMetadata/v1/", lambda host: addresses)


def test_an_unresolvable_host_is_refused() -> None:
    def fail(host: str) -> list[str]:
        raise OSError("no such host")

    with pytest.raises(ValueError, match="could not be resolved"):
        public_http_url("https://nowhere.invalid/", fail)


def test_httpx_fetcher_reads_the_client(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello page", headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    page = HttpxFetcher(client, resolve=_public).fetch("https://example.test/a")
    assert page.status == 200
    assert page.body == "hello page"
    client.close()
