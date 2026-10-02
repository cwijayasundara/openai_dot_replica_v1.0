"""Does a replayed proposal match what the human did? Code decides, one rule per human action.

The default compares each argument: strings match when they are close
(normalised ``difflib`` ratio of at least 0.8), anything else must be equal.
A tool may register rules for its own fields. Email bodies are judged on
length, because that is what a human usually changes, not the exact wording.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dot.persistence.db import Json

FieldRule = Callable[[Any, Any], bool]

TEXT_RATIO = 0.8
WORD_TOLERANCE = 0.25


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def close_text(got: Any, want: Any) -> bool:
    if not isinstance(got, str) or not isinstance(want, str):
        return bool(got == want)
    return difflib.SequenceMatcher(None, _normal(got), _normal(want)).ratio() >= TEXT_RATIO


def similar_length(got: Any, want: Any) -> bool:
    """Word counts within 25% of the target's."""
    if not isinstance(got, str) or not isinstance(want, str):
        return False
    target = len(want.split())
    return abs(len(got.split()) - target) <= WORD_TOLERANCE * max(target, 1)


def same_address(got: Any, want: Any) -> bool:
    return isinstance(got, str) and isinstance(want, str) and _normal(got) == _normal(want)


_EMAIL: dict[str, FieldRule] = {"to": same_address, "body": similar_length}
FIELD_RULES: dict[str, dict[str, FieldRule]] = {"send_email": _EMAIL, "draft_email": _EMAIL}


def args_match(tool: str, got: Json, want: Json) -> bool:
    if set(got) != set(want):
        return False
    rules = FIELD_RULES.get(tool, {})
    return all(rules.get(key, close_text)(got[key], want[key]) for key in want)


@dataclass(frozen=True)
class Expectation:
    """What the human's action says the dot should (or should not) propose."""

    action: str
    calls: tuple[Json, ...]

    def met_by(self, proposed: list[Json], stop: str) -> bool:
        if stop == "budget":
            return False
        made = [
            any(c["name"] == call["name"] and args_match(c["name"], c["args"], call["args"]) for c in proposed)
            for call in self.calls
        ]
        if self.action in {"approve", "edit"}:
            return all(made)
        # A rejection or correction is met when the judged calls are not made again.
        return not any(made)


def expectation(action: str, calls: list[Json], outcome: Json) -> Expectation:
    """From an episode's action, the calls it judged, and its outcome."""
    if action == "edit":
        edited = outcome.get("decision", {}).get("edited_args")
        if not isinstance(edited, dict) or len(calls) != 1:
            raise ValueError("an edit episode needs one call and its edited arguments")
        return Expectation(action, ({"name": calls[0]["name"], "args": edited},))
    if action in {"approve", "reject", "correction"}:
        return Expectation(action, tuple(calls))
    raise ValueError(f"unknown action {action!r}")
