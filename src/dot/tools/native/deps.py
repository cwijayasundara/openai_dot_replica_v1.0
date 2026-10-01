"""Seams the native tools call. Tests pass fakes; nothing here opens a network connection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dot.tools.artifacts import ArtifactStore

SMTP_CREDENTIAL = "cred:smtp"


class CredentialError(Exception):
    """The broker could not resolve a handle. The message must not contain the secret."""


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    snippet: str


class SearchClient(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[Hit]: ...


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    body: str


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


class CredentialBroker(Protocol):
    def resolve(self, handle: str) -> str: ...


class EmailTransport(Protocol):
    def send(self, *, to: str, subject: str, body: str, credential: str) -> str: ...


class SlackPoster(Protocol):
    def post(self, *, channel: str, text: str) -> str: ...


@dataclass
class ToolDeps:
    artifacts: ArtifactStore
    search: SearchClient | None = None
    fetcher: PageFetcher | None = None
    credentials: CredentialBroker | None = None
    email: EmailTransport | None = None
    slack: SlackPoster | None = None
