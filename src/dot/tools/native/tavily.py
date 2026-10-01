"""Tavily web search. The API key travels in a header and never reaches the model."""

from __future__ import annotations

from typing import Any

import httpx

from dot.tools.native.deps import Hit

TAVILY_URL = "https://api.tavily.com/search"
_TIMEOUT_S = 20.0


class SearchError(Exception):
    """Search failed. The message names the failure, never the key."""


class TavilySearch:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("a Tavily API key is required")
        self._key = api_key
        self._client = client

    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        client = self._client or httpx.Client(timeout=_TIMEOUT_S)
        try:
            response = client.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                json={"query": query, "max_results": max(1, min(limit, 10)), "search_depth": "basic"},
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"search request failed ({type(exc).__name__})") from None
        finally:
            if self._client is None:
                client.close()
        if response.status_code != 200:
            raise SearchError(f"search returned HTTP {response.status_code}")
        return [_hit(item) for item in response.json().get("results", [])[:limit] if isinstance(item, dict)]


def _hit(item: dict[str, Any]) -> Hit:
    return Hit(title=str(item.get("title", "")), url=str(item.get("url", "")), snippet=str(item.get("content", "")))
