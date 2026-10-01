"""Web identity: a fixed local user, or a verified IAP assertion. Never a client header."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from google.auth import jwt
from google.auth.crypt import es256
from pydantic import ValidationError

from dot.assembly import build_graph_runtime
from dot.config import Settings
from dot.persistence.db import MemoryRepositories, User
from dot.runtime.turns import InMemoryEventChannel
from dot.surfaces.api import create_app
from dot.surfaces.identity import IAP_HEADER, IAP_ISSUER, IapVerifier

AUDIENCE = "/projects/1/global/backendServices/2"


def _settings(tmp_path: Path, **values: Any) -> Settings:
    return Settings(_env_file=None, database_url=None, object_root=str(tmp_path), **values)  # type: ignore[call-arg]


def _pem(keys: _Keys) -> bytes:
    return keys.private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class _Keys:
    def __init__(self) -> None:
        private = ec.generate_private_key(ec.SECP256R1())
        self.private = private
        self.signer = es256.ES256Signer.from_string(_pem(self), key_id="k1")
        public = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.certs = {"k1": public.decode()}
        self.fetches = 0

    def fetch(self) -> Mapping[str, str]:
        self.fetches += 1
        return self.certs

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        payload = {"iss": IAP_ISSUER, "aud": AUDIENCE, "sub": "accounts.google.com:42", "iat": now, "exp": now + 600}
        payload.update(claims)
        return jwt.encode(self.signer, payload).decode()  # type: ignore[no-untyped-call]


def _client(tmp_path: Path, settings: Settings, repos: MemoryRepositories, keys: _Keys | None = None) -> TestClient:
    app = create_app(
        settings,
        repos=repos,
        runtime=build_graph_runtime(settings),
        events=InMemoryEventChannel(),
        iap_verifier=IapVerifier(AUDIENCE, keys.fetch) if keys is not None else None,
    )
    return TestClient(app)


def test_off_trusts_no_header(tmp_path: Path) -> None:
    with _client(tmp_path, _settings(tmp_path), MemoryRepositories()) as client:
        assert client.get("/me", headers={"x-user-id": "ada", IAP_HEADER: "forged"}).status_code == 401


def test_dev_mode_is_one_fixed_local_user(tmp_path: Path) -> None:
    settings = _settings(tmp_path, web_auth="dev", web_dev_user="ada")
    with _client(tmp_path, settings, MemoryRepositories()) as client:
        assert client.get("/me").json() == {"user_id": "ada"}


def test_dev_mode_is_refused_outside_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="DOT_ENV=local"):
        _settings(tmp_path, web_auth="dev", web_dev_user="ada", env="cloud")
    monkeypatch.setenv("K_SERVICE", "dot-api")
    with pytest.raises(ValidationError, match="Cloud Run"):
        _settings(tmp_path, web_auth="dev", web_dev_user="ada")
    monkeypatch.delenv("K_SERVICE")
    with pytest.raises(ValidationError, match="DOT_WEB_DEV_USER"):
        _settings(tmp_path, web_auth="dev")
    with pytest.raises(ValidationError, match="DOT_IAP_AUDIENCE"):
        _settings(tmp_path, web_auth="iap")


def test_iap_maps_a_verified_subject_to_its_linked_user(tmp_path: Path) -> None:
    repos = MemoryRepositories()
    repos.create_user(User("ada", "Ada", web_subject="accounts.google.com:42"))
    keys = _Keys()
    settings = _settings(tmp_path, web_auth="iap", iap_audience=AUDIENCE, env="cloud")
    with _client(tmp_path, settings, repos, keys) as client:
        assert client.get("/me", headers={IAP_HEADER: keys.token()}).json() == {"user_id": "ada"}
        assert client.get("/me", headers={IAP_HEADER: keys.token()}).status_code == 200
        assert keys.fetches == 1  # keys are cached

        assert client.get("/me").status_code == 401
        assert client.get("/me", headers={IAP_HEADER: "not-a-jwt"}).status_code == 401
        assert client.get("/me", headers={IAP_HEADER: keys.token(aud="/other")}).status_code == 401
        assert client.get("/me", headers={IAP_HEADER: keys.token(iss="https://evil")}).status_code == 401
        assert client.get("/me", headers={IAP_HEADER: keys.token(sub="accounts.google.com:7")}).status_code == 401
        expired = keys.token(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)
        assert client.get("/me", headers={IAP_HEADER: expired}).status_code == 401
        forged = _Keys().token()
        assert client.get("/me", headers={IAP_HEADER: forged}).status_code == 401


def test_a_key_fetch_failure_fails_closed_and_recovers(tmp_path: Path) -> None:
    repos = MemoryRepositories()
    repos.create_user(User("ada", "Ada", web_subject="accounts.google.com:42"))
    keys = _Keys()
    down = [True]

    def fetch() -> Mapping[str, str]:
        if down[0]:
            raise httpx.ConnectError("gstatic unreachable")
        return keys.fetch()

    settings = _settings(tmp_path, web_auth="iap", iap_audience=AUDIENCE, env="cloud")
    app = create_app(
        settings,
        repos=repos,
        runtime=build_graph_runtime(settings),
        events=InMemoryEventChannel(),
        iap_verifier=IapVerifier(AUDIENCE, fetch),
    )
    with TestClient(app) as client:
        assert client.get("/me", headers={IAP_HEADER: keys.token()}).status_code == 401
        down[0] = False
        assert client.get("/me", headers={IAP_HEADER: keys.token()}).status_code == 200


def test_a_rotated_key_is_fetched_once_and_forged_ids_cannot_force_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("dot.surfaces.identity.time.monotonic", lambda: now[0])
    old, new = _Keys(), _Keys()
    new.signer = es256.ES256Signer.from_string(_pem(new), key_id="k2")
    published = [dict(old.certs)]
    fetches = []

    def fetch() -> Mapping[str, str]:
        fetches.append(now[0])
        return published[0]

    verifier = IapVerifier(AUDIENCE, fetch)
    assert verifier.subject(old.token()) == "accounts.google.com:42"
    published[0] = {**old.certs, "k2": new.certs["k1"]}
    # Within a minute of the last fetch an unknown key id is just rejected.
    assert verifier.subject(new.token()) is None
    now[0] += 61
    assert verifier.subject(new.token()) == "accounts.google.com:42"
    assert verifier.subject(new.token()) == "accounts.google.com:42"
    assert len(fetches) == 2
