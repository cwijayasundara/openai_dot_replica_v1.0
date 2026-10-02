"""Seams the native tools call. Tests pass fakes; nothing here opens a network connection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from dot.tools.artifacts import ArtifactStore

if TYPE_CHECKING:
    from dot.tools.native.recon_client import ReconClient

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
    recon: ReconClient | None = None
    drop_root: Path | None = None
    # Files this dot was refused permission to start: (sponsor_id, file_name, sha256).
    recon_declined: Callable[[], frozenset[tuple[str, str, str]]] | None = None
    # Reads one of this dot's wiki pages by path, such as "/wiki/sponsors.md". None when missing.
    wiki_page: Callable[[str], str | None] | None = None
