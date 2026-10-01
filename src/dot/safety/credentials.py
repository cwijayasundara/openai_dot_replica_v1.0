"""Allowlisted credential handles; values never become tool arguments."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import google_crc32c
from google.cloud import secretmanager

from dot.config import Settings
from dot.middleware.redaction import Redactor
from dot.tools.native.deps import CredentialBroker as BrokerProtocol
from dot.tools.native.deps import CredentialError


class SecretSource(Protocol):
    def read(self, reference: str) -> str: ...


class EnvironmentSecrets:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def read(self, reference: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", reference):
            raise CredentialError("invalid environment binding")
        value = self._environment.get(reference)
        if not value:
            raise CredentialError("credential is not configured")
        return value


class SecretManagerSecrets:
    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._factory = client_factory or secretmanager.SecretManagerServiceClient

    def read(self, reference: str) -> str:
        if not re.fullmatch(r"projects/[^/]+/secrets/[^/]+/versions/(?:[1-9][0-9]*|latest)", reference):
            raise CredentialError("invalid Secret Manager binding")
        # ADC is resolved lazily, in tool code, not during graph assembly.
        with self._factory() as client:
            result = client.access_secret_version(request={"name": reference}, timeout=10.0, retry=None)
        data = bytes(result.payload.data)
        if google_crc32c.value(data) != result.payload.data_crc32c:
            raise CredentialError("credential payload integrity check failed")
        return data.decode("utf-8")


class CredentialBroker:
    def __init__(self, bindings: Mapping[str, str], source: SecretSource, redactor: Redactor) -> None:
        if any(not re.fullmatch(r"cred:[a-z][a-z0-9_-]*", handle) for handle in bindings):
            raise ValueError("invalid credential handle binding")
        self._bindings = dict(bindings)
        self._source = source
        self._redactor = redactor

    def resolve(self, handle: str) -> str:
        reference = self._bindings.get(handle)
        if reference is None:
            raise CredentialError("unknown credential handle")
        try:
            value = self._source.read(reference)
            if not value:
                raise CredentialError("empty credential")
        except Exception:
            # Never propagate provider errors or values through exception text.
            raise CredentialError("could not resolve credential") from None
        self._redactor.add(value)
        return value


class RedactingBroker:
    """Apply the same boundary to injected transports/brokers in tests or adapters."""

    def __init__(self, broker: BrokerProtocol, redactor: Redactor) -> None:
        self._broker, self._redactor = broker, redactor

    def resolve(self, handle: str) -> str:
        try:
            value = self._broker.resolve(handle)
            if not value:
                raise CredentialError("empty credential")
        except Exception:
            raise CredentialError("could not resolve credential") from None
        self._redactor.add(value)
        return value


def build_credential_broker(settings: Settings, redactor: Redactor) -> CredentialBroker:
    if settings.credential_backend == "secret_manager":
        return CredentialBroker(settings.credential_bindings, SecretManagerSecrets(), redactor)
    environment = dict(os.environ)
    # Pydantic loads these known fields from .env without mutating os.environ.
    for name, value in (
        ("DOT_SMTP_CREDENTIAL", settings.smtp_credential),
        ("DOT_SLACK_BOT_TOKEN", settings.slack_bot_token),
    ):
        if value:
            environment[name] = value
    return CredentialBroker(settings.credential_bindings, EnvironmentSecrets(environment), redactor)
