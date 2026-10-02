from __future__ import annotations

import httpx
import pytest

from dot.tools.native.deps import CredentialError
from dot.tools.native.recon_client import RECON_CREDENTIAL, ReconClient, ReconError


class _Broker:
    def __init__(self, token: str | None) -> None:
        self.token, self.handles = token, []

    def resolve(self, handle: str) -> str:
        self.handles.append(handle)
        if self.token is None:
            raise CredentialError("could not resolve credential")
        return self.token


def _client(handler, *, credentials=None):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = ReconClient("http://recon.test", credentials=credentials, transport=httpx.MockTransport(record))
    return client, seen


def test_reads_hit_the_right_paths() -> None:
    client, seen = _client(
        lambda r: httpx.Response(200, json=[{"id": "x"}] if r.url.path != "/runs/run-1" else {"id": "run-1"})
    )
    assert client.sponsors() == [{"id": "x"}]
    assert client.runs("sponsor-a") == [{"id": "x"}]
    assert client.runs() == [{"id": "x"}]
    assert client.run("run-1") == {"id": "run-1"}
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/sponsors"),
        ("GET", "/runs"),
        ("GET", "/runs"),
        ("GET", "/runs/run-1"),
    ]
    assert seen[1].url.params["sponsor_id"] == "sponsor-a"
    assert "sponsor_id" not in seen[2].url.params


def test_start_posts_multipart_and_returns_run_id() -> None:
    client, seen = _client(lambda r: httpx.Response(200, json={"run_id": "run-9"}))
    assert client.start("sponsor-a", "a.csv", b"col\n1\n") == "run-9"
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/runs")
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="sponsor_id"' in body and b"sponsor-a" in body
    assert b'name="entity"' in body and b"affiliate" in body
    assert b'filename="a.csv"' in body and b"col\n1\n" in body


def test_actor_header_and_bearer_token() -> None:
    broker = _Broker("tok")
    client, seen = _client(lambda r: httpx.Response(200, json=[]), credentials=broker)
    client.sponsors()
    assert seen[0].headers["x-actor"] == "dot"
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert broker.handles == [RECON_CREDENTIAL]


def test_no_auth_header_when_credential_unresolved_or_absent() -> None:
    for credentials in (_Broker(None), None):
        client, seen = _client(lambda r: httpx.Response(200, json=[]), credentials=credentials)
        client.sponsors()
        assert seen[0].headers["x-actor"] == "dot"
        assert "authorization" not in seen[0].headers


def test_with_credentials_late_binds_the_broker() -> None:
    client, seen = _client(lambda r: httpx.Response(200, json=[]))
    client.with_credentials(_Broker("late")).sponsors()
    assert seen[0].headers["authorization"] == "Bearer late"


def test_non_2xx_raises_with_clipped_detail() -> None:
    client, _ = _client(lambda r: httpx.Response(422, json={"detail": "unsupported file type"}))
    with pytest.raises(ReconError, match="workbench returned 422: unsupported file type"):
        client.sponsors()
    client, _ = _client(lambda r: httpx.Response(500, json={"detail": "x" * 1000}))
    with pytest.raises(ReconError) as err:
        client.sponsors()
    assert len(str(err.value)) < 350
    client, _ = _client(lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(ReconError, match="workbench returned 502"):
        client.sponsors()


def test_error_never_carries_the_token() -> None:
    client, _ = _client(lambda r: httpx.Response(401, json={"detail": "denied"}), credentials=_Broker("s3cret"))
    with pytest.raises(ReconError) as err:
        client.sponsors()
    assert "s3cret" not in str(err.value)


def test_transport_error_is_unreachable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused for tok")

    client, _ = _client(boom)
    with pytest.raises(ReconError) as err:
        client.sponsors()
    assert str(err.value) == "workbench unreachable"
    assert err.value.__cause__ is None


def test_client_has_no_gate_surface() -> None:
    assert not [n for n in dir(ReconClient) if "gate" in n]
