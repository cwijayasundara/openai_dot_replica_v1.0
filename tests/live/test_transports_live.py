"""Live transports: a real Tavily search and a real fetch through the DNS check."""

from __future__ import annotations

import pytest

from dot.config import get_settings
from dot.tools.native.fetch import HttpxFetcher
from dot.tools.native.tavily import TavilySearch

pytestmark = pytest.mark.live


def test_tavily_returns_results() -> None:
    key = get_settings().tavily_api_key
    assert key, "set DOT_TAVILY_API_KEY to run the live search check"
    hits = TavilySearch(key).search("LangGraph checkpointer Postgres", limit=3)
    assert hits and all(hit.url.startswith("http") and hit.title for hit in hits)


def test_fetch_resolves_and_reads_a_public_page() -> None:
    page = HttpxFetcher().fetch("https://example.com/")
    assert page.status == 200 and "Example Domain" in page.body
