"""Client for the recon workbench HTTP API.

Deliberately read-and-start only: there is no gate method, so this dot can never answer a
workbench gate. A human does that in the workbench.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx

from dot.tools.native.deps import CredentialBroker, CredentialError

RECON_CREDENTIAL = "cred:recon"
_DETAIL_LIMIT = 300


class ReconError(Exception):
    """The workbench refused or could not be reached. The message never contains a credential."""


class ReconClient:
    def __init__(
        self,
        base_url: str,
        *,
        credentials: CredentialBroker | None,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._timeout_s = timeout_s
        self._transport = transport

    def with_credentials(self, credentials: CredentialBroker | None) -> ReconClient:
        """The same client reading its token from ``credentials``.

        Assembly wraps the broker in a redacting one after the deps are built.
        """
        return ReconClient(
            self._base_url, credentials=credentials, timeout_s=self._timeout_s, transport=self._transport
        )

    def sponsors(self) -> list[dict[str, Any]]:
        sponsors: list[dict[str, Any]] = self._request("GET", "/sponsors")
        return sponsors

    def runs(self, sponsor_id: str | None = None) -> list[dict[str, Any]]:
        params = {"sponsor_id": sponsor_id} if sponsor_id else None
        runs: list[dict[str, Any]] = self._request("GET", "/runs", params=params)
        return runs

    def run(self, run_id: str) -> dict[str, Any]:
        run: dict[str, Any] = self._request("GET", f"/runs/{run_id}")
        return run

    def start(self, sponsor_id: str, file_name: str, data: bytes) -> str:
        body = self._request(
            "POST",
            "/runs",
            data={"sponsor_id": sponsor_id, "entity": "affiliate"},
            files={"file": (file_name, data)},
        )
        return str(body["run_id"])

    def _headers(self) -> dict[str, str]:
        headers = {"X-Actor": "dot"}
        if self._credentials is not None:
            # An unresolved credential means a local workbench with no token.
            with contextlib.suppress(CredentialError):
                headers["Authorization"] = f"Bearer {self._credentials.resolve(RECON_CREDENTIAL)}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
                response = client.request(method, self._base_url + path, headers=self._headers(), **kwargs)
        except httpx.HTTPError:
            raise ReconError("workbench unreachable") from None
        if not response.is_success:
            raise ReconError(f"workbench returned {response.status_code}: {_detail(response)}")
        try:
            return response.json()
        except ValueError:
            raise ReconError("workbench returned an unreadable response") from None


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = ""
    return str(detail)[:_DETAIL_LIMIT]
