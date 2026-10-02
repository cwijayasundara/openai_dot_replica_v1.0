"""L4: memory versions. Every judged edit is a ``memory_versions`` row; humans can undo or settle them.

- The gate's verdict is recorded here, and an accepted edit is applied.
- An approver can roll back an accepted edit, or accept or discard one the
  gate held as ``needs_review``. Each action changes the row and appends an
  audit event in one transaction, and only if the row is still in the status
  the action expects.

Everything here runs under the dot's lock. The store is written before the
row: if the row update fails, the file has changed but the row has not. The
next gate or action then finds the edit already in the file, or its find
text gone, and refuses rather than applying it twice.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dot.memory.reflection import DraftEdit, MemoryEdit, MemoryFiles, check_edit, sha256
from dot.middleware.redaction import Redactor
from dot.persistence.db import AuditEvent, Json, MemoryConflict, MemoryVersion, Repositories

if TYPE_CHECKING:
    from dot.memory.replay import GateResult


def recorded_edit(version: MemoryVersion, files: dict[str, str], redactor: Redactor) -> MemoryEdit | str:
    """The version's edit applied to the files as they are now, or why it no longer applies."""
    detail = version.detail
    if not all(isinstance(detail.get(key), str) for key in ("path", "find", "replace")):
        return "the version does not record its edit"
    draft = DraftEdit(
        path=detail["path"],
        find=detail["find"],
        replace=detail["replace"],
        rationale=str(detail.get("rationale") or "-"),
        episode_ids=version.episodes or [0],
    )
    current = files.get(draft.path)
    # An edit that keeps its find text (appending a line, say) would still apply after it
    # already has; a file that already holds the replacement means it landed before.
    if draft.find and draft.find in draft.replace and current is not None and draft.replace in current:
        return "the edit is already in the file"
    checked = check_edit(draft, files, set(draft.episode_ids), redactor)
    return checked if isinstance(checked, MemoryEdit) else checked.reason


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
        detail |= _applied(files, result.edit)
    judged = replace(version, status=result.status, diff=diff, detail=detail)
    repos.update_memory_version(judged)
    return judged


def accept_reviewed(
    repos: Repositories, files: MemoryFiles, version: MemoryVersion, user_id: str, redactor: Redactor
) -> MemoryVersion:
    """A human accepts an edit the gate could not judge. It must still apply to the file as it is now."""
    _expect(version, "needs_review")
    edit = recorded_edit(version, files.listing(), redactor)
    if isinstance(edit, str):
        raise MemoryConflict(f"the edit no longer applies: {edit}")
    detail = {**version.detail, "base_sha256": edit.base_sha256, **_applied(files, edit)}
    accepted = replace(version, status="accepted", diff=edit.diff, detail=_reviewed(detail, user_id, "accept"))
    repos.transition_memory_version(accepted, "needs_review", _event(accepted, user_id, "accept", redactor))
    return accepted


def discard(repos: Repositories, version: MemoryVersion, user_id: str, redactor: Redactor) -> MemoryVersion:
    _expect(version, "needs_review")
    discarded = replace(version, status="discarded", detail=_reviewed(version.detail, user_id, "discard"))
    repos.transition_memory_version(discarded, "needs_review", _event(discarded, user_id, "discard", redactor))
    return discarded


def rollback(
    repos: Repositories, files: MemoryFiles, version: MemoryVersion, user_id: str, redactor: Redactor
) -> MemoryVersion:
    """Undo an accepted edit.

    When nothing has changed the file since, it is restored exactly (a file the
    edit created is removed). Otherwise the edit's own text is swapped back,
    which needs that text to still be there, once. Anything else is a conflict:
    a later edit touched the same text.
    """
    _expect(version, "accepted")
    detail = version.detail
    path, find, replaced = detail.get("path"), detail.get("find"), detail.get("replace")
    if not isinstance(path, str) or not isinstance(find, str) or not isinstance(replaced, str):
        raise MemoryConflict("the version does not record its edit")
    current = files.read(path)
    if current is not None and sha256(current) == detail.get("after_sha256"):
        before = detail.get("before")
        if isinstance(before, str):
            files.write(path, before)
        else:
            files.delete(path)
    elif current is not None and replaced and current.count(replaced) == 1:
        if _built_on(repos, version, path, replaced):
            raise MemoryConflict("a later edit is built on this one; roll that back first")
        files.write(path, current.replace(replaced, find))
    else:
        raise MemoryConflict("a later edit changed the text this edit wrote")
    rolled = replace(version, status="rolled_back", detail=_reviewed(detail, user_id, "rollback"))
    repos.transition_memory_version(rolled, "accepted", _event(rolled, user_id, "rollback", redactor))
    return rolled


def _built_on(repos: Repositories, version: MemoryVersion, path: str, replaced: str) -> bool:
    """A newer accepted edit to the same file whose find text contains this edit's text.

    Swapping this edit's text back would rewrite part of that edit, which
    could then never be rolled back.
    """
    return any(
        later.id > version.id
        and later.status == "accepted"
        and later.detail.get("path") == path
        and isinstance(later.detail.get("find"), str)
        and replaced in later.detail["find"]
        for later in repos.list_memory_versions(version.dot_id)
    )


def _applied(files: MemoryFiles, edit: MemoryEdit) -> Json:
    """Write the edit; keep what rollback needs to restore the file exactly."""
    files.write(edit.path, edit.after)
    return {"before": edit.before, "after_sha256": sha256(edit.after)}


def _expect(version: MemoryVersion, status: str) -> None:
    if version.status != status:
        raise MemoryConflict(f"memory version {version.id} is {version.status}, not {status}")


def _reviewed(detail: Json, user_id: str, action: str) -> Json:
    return {**detail, action: {"by": user_id, "at": datetime.now(UTC).isoformat()}}


def _event(version: MemoryVersion, user_id: str, action: str, redactor: Redactor) -> AuditEvent:
    return AuditEvent(
        0,
        version.dot_id,
        datetime.now(UTC),
        user_id,
        "memory",
        decision=action,
        detail=redactor.content({"version": version.id, "path": version.detail.get("path"), "status": version.status}),
    )
