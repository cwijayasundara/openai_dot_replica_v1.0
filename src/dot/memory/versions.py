"""L4: memory versions. Every judged edit is a ``memory_versions`` row; an accepted one is applied.

Runs under the dot's lock. The store is written before the row: if the row
update fails, the edit is in memory but its row still says ``proposed``, and
the next gate rejects it as a stale base (its find text is gone) rather than
applying it twice.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from dot.memory.reflection import MemoryFiles
from dot.persistence.db import Json, MemoryVersion, Repositories

if TYPE_CHECKING:
    from dot.memory.replay import GateResult


def record_verdict(
    repos: Repositories, files: MemoryFiles, version: MemoryVersion, result: GateResult
) -> MemoryVersion:
    gate: Json = {
        "reason": result.reason,
        "results": [r.view() for r in result.results],
        "unreplayable": [{"episode": u.episode_id, "reason": u.reason} for u in result.unreplayable],
    }
    detail: Json = {**version.detail, "gate": gate}
    diff = version.diff
    if result.edit is not None:
        # The diff and base as judged: an earlier edit tonight may have moved the file.
        diff = result.edit.diff
        detail["base_sha256"] = result.edit.base_sha256
    if result.status == "accepted":
        if result.edit is None:
            raise ValueError("an accepted verdict needs its edit")
        files.write(result.edit.path, result.edit.after)
    judged = replace(version, status=result.status, diff=diff, detail=detail)
    repos.update_memory_version(judged)
    return judged
