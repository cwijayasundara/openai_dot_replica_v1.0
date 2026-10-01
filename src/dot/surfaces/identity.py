"""Who is calling the API from the web. Sets ``request.state.user_id`` or leaves it unset.

Routes that need a principal answer 401 when it is unset. Identity is never
read from the request body or from a header the client can forge: ``dev`` is
a fixed local user, and ``iap`` trusts only a JWT signed by Google's IAP.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from fastapi import FastAPI, Request
from google.auth import exceptions as google_exceptions
from google.auth import jwt
from starlette.responses import Response

from dot.config import Settings
from dot.persistence.db import NotFound, Repositories

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_CERTS_TTL_S = 3600.0
_REFETCH_MIN_S = 60.0

log = logging.getLogger(__name__)

CertsFetcher = Callable[[], Mapping[str, str]]


def _fetch_certs(url: str) -> Mapping[str, str]:
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    certs = response.json()
    if not isinstance(certs, dict):
        raise ValueError("signing certificates are not a key map")
    return {str(key): str(value) for key, value in certs.items()}


def fetch_iap_certs() -> Mapping[str, str]:
    """IAP's signing keys as ``{key id: PEM}``."""
    return _fetch_certs(IAP_CERTS_URL)


def fetch_google_certs() -> Mapping[str, str]:
    """Google's OIDC signing certificates as ``{key id: PEM}``."""
    return _fetch_certs(GOOGLE_CERTS_URL)


class _GoogleSignedJwt:
    """Signature and audience checks against Google's keys. Keys are cached for an hour."""

    def __init__(self, audience: str, fetch: CertsFetcher) -> None:
        self._audience = audience
        self._fetch = fetch
        self._lock = threading.Lock()
        self._certs: Mapping[str, str] = {}
        self._fetched_at = 0.0

    def _claims(self, token: str) -> Mapping[str, Any] | None:
        """Verified claims, or None. Never raises: a fetch failure fails closed."""
        try:
            return self._verify(token)
        except (google_exceptions.GoogleAuthError, ValueError, httpx.HTTPError) as exc:
            log.warning("rejected %s token: %s", type(self).__name__, exc)
            return None

    def _verify(self, token: str) -> Mapping[str, Any]:
        try:
            return self._decode(token, self._keys())
        except google_exceptions.MalformedError as exc:
            # Google rotates keys: an unknown key id may be a new key, so refetch once.
            if "not found" not in str(exc) or not self._refetch():
                raise
            return self._decode(token, self._keys())

    def _decode(self, token: str, keys: Mapping[str, str]) -> Mapping[str, Any]:
        claims: Mapping[str, Any] = jwt.decode(token, certs=keys, audience=self._audience)  # type: ignore[no-untyped-call]
        return claims

    def _keys(self) -> Mapping[str, str]:
        with self._lock:
            if not self._certs or time.monotonic() - self._fetched_at > _CERTS_TTL_S:
                self._load()
            return self._certs

    def _refetch(self) -> bool:
        """Refetch the keys unless that happened in the last minute, so forged key ids cannot force fetches."""
        with self._lock:
            if time.monotonic() - self._fetched_at < _REFETCH_MIN_S:
                return False
            self._load()
            return True

    def _load(self) -> None:
        self._certs = self._fetch()
        self._fetched_at = time.monotonic()


class IapVerifier(_GoogleSignedJwt):
    """Checks the IAP assertion's signature, issuer and audience."""

    def __init__(self, audience: str, fetch: CertsFetcher = fetch_iap_certs) -> None:
        super().__init__(audience, fetch)

    def subject(self, token: str) -> str | None:
        """The verified subject, or None."""
        claims = self._claims(token)
        if claims is None:
            return None
        if claims.get("iss") != IAP_ISSUER:
            log.warning("rejected IAP assertion from issuer %r", claims.get("iss"))
            return None
        subject = claims.get("sub")
        return subject if isinstance(subject, str) and subject else None


class SchedulerVerifier(_GoogleSignedJwt):
    """Cloud Scheduler's OIDC token: Google-signed, for our audience, naming our invoker account."""

    def __init__(self, audience: str, invoker: str, fetch: CertsFetcher = fetch_google_certs) -> None:
        super().__init__(audience, fetch)
        self._invoker = invoker

    def allows(self, token: str) -> bool:
        claims = self._claims(token)
        if claims is None:
            return False
        if claims.get("iss") not in GOOGLE_ISSUERS:
            log.warning("rejected scheduler token from issuer %r", claims.get("iss"))
            return False
        if claims.get("email") != self._invoker or claims.get("email_verified") is not True:
            log.warning("rejected scheduler token for %r", claims.get("email"))
            return False
        return True


def install_identity(
    app: FastAPI, settings: Settings, repos: Repositories, verifier: IapVerifier | None = None
) -> None:
    """Add the middleware for ``settings.web_auth``. ``off`` adds nothing."""
    if settings.web_auth == "off":
        return
    if settings.web_auth == "dev":
        user_id = settings.web_dev_user
        assert user_id is not None  # Settings refuses dev auth without it.

        async def resolve(request: Request) -> str | None:
            del request
            return user_id

    else:
        assert settings.iap_audience is not None  # Settings refuses iap auth without it.
        iap = verifier or IapVerifier(settings.iap_audience)

        async def resolve(request: Request) -> str | None:
            token = request.headers.get(IAP_HEADER)
            if not token:
                return None
            return await asyncio.to_thread(_iap_user, iap, repos, token)

    @app.middleware("http")
    async def identity(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        user_id = await resolve(request)
        if user_id is not None:
            request.state.user_id = user_id
        return await call_next(request)


def _iap_user(verifier: IapVerifier, repos: Repositories, token: str) -> str | None:
    subject = verifier.subject(token)
    if subject is None:
        return None
    try:
        return repos.find_user_by_web_subject(subject).user_id
    except NotFound:
        log.warning("IAP subject %s has no linked user", subject)
        return None


def principal(request: Request) -> str | None:
    user_id: Any = getattr(request.state, "user_id", None)
    return user_id if isinstance(user_id, str) and user_id else None
