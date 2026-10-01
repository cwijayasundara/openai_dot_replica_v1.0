"""Marker wrapped around text that did not come from the user or the pack."""

from __future__ import annotations

UNTRUSTED_MARKER = "untrusted-data"
EXCERPT_CHARS = 4_000


def envelope(text: str, *, source: str, limit: int = EXCERPT_CHARS) -> dict[str, object]:
    """Return a bounded excerpt the model must treat as data, not instructions."""
    return {
        "marker": UNTRUSTED_MARKER,
        "source": source,
        "truncated": len(text) > limit,
        "text": text[:limit],
    }
