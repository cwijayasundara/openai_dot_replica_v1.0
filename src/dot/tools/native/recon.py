"""Recon workbench tools: read drops and runs, and start a run on an approved file.

No tool here reaches a workbench gate; the client has no method for it. File
contents never reach the model, only names, sizes and hashes.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from dot.tools.native.deps import ToolDeps
from dot.tools.native.recon_client import ReconError
from dot.tools.results import fail, ok

SUPPORTED = (".csv", ".tsv", ".xlsx", ".xls")
MAX_BYTES = 20 * 1024 * 1024
SPONSOR_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

NOT_CONFIGURED = "the recon workbench is not configured"
_CHUNK = 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _read_no_follow(path: Path) -> bytes:
    # O_NOFOLLOW closes the gap between the symlink check and the read.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _hours_since(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - then).total_seconds() / 3600, 1)


def _run_id(run: dict[str, Any]) -> str | None:
    value = run.get("id", run.get("run_id"))
    return None if value is None else str(value)


def _size_reason(name: str, size: int) -> str | None:
    if Path(name).suffix.lower() not in SUPPORTED:
        return "unsupported type"
    if size > MAX_BYTES:
        return "too large"
    return None


def _sponsor_dirs(root: Path, sponsor_id: str | None) -> list[Path]:
    candidates = [root / sponsor_id] if sponsor_id else sorted(root.iterdir()) if root.is_dir() else []
    return [path for path in candidates if SPONSOR_ID.fullmatch(path.name) and not path.is_symlink() and path.is_dir()]


def build_list_sponsors(deps: ToolDeps) -> BaseTool:
    def list_sponsors() -> str:
        """List the sponsors registered in the recon workbench, as id and name."""
        if deps.recon is None:
            return fail(NOT_CONFIGURED)
        try:
            sponsors = deps.recon.sponsors()
        except ReconError as exc:
            return fail(str(exc))
        return ok(sponsors=[{"id": s.get("id"), "name": s.get("name")} for s in sponsors])

    return StructuredTool.from_function(list_sponsors, name="list_sponsors")


def build_list_drops(deps: ToolDeps) -> BaseTool:
    def list_drops(sponsor_id: str | None = None) -> str:
        """List files in the sponsor drop folders with size, sha256, whether they can be
        uploaded, any existing run_id, and whether a start was declined. Optionally for one sponsor."""
        if deps.recon is None or deps.drop_root is None:
            return fail(NOT_CONFIGURED)
        if sponsor_id is not None and not SPONSOR_ID.fullmatch(sponsor_id):
            return fail("invalid sponsor id")
        try:
            known = {str(s.get("id")) for s in deps.recon.sponsors()}
            runs = deps.recon.runs()
        except ReconError as exc:
            return fail(str(exc))
        by_sha = {str(r.get("upload_sha")): _run_id(r) for r in runs if r.get("upload_sha")}
        declined = deps.recon_declined() if deps.recon_declined is not None else frozenset()
        files: list[dict[str, Any]] = []
        for folder in _sponsor_dirs(deps.drop_root, sponsor_id):
            for path in sorted(folder.iterdir()):
                entry: dict[str, Any] = {"sponsor_id": folder.name, "file_name": path.name}
                if path.is_symlink():
                    entry |= {"bytes": None, "sha256": None, "supported": False, "reason": "symlink"}
                    entry |= {"run_id": None, "declined": False}
                    files.append(entry)
                    continue
                if not path.is_file():
                    continue
                size = path.stat().st_size
                sha = _sha256(path)
                reason = "unknown sponsor" if folder.name not in known else _size_reason(path.name, size)
                entry |= {"bytes": size, "sha256": sha, "supported": reason is None, "reason": reason}
                entry |= {"run_id": by_sha.get(sha), "declined": (folder.name, path.name, sha) in declined}
                files.append(entry)
        return ok(files=files)

    return StructuredTool.from_function(list_drops, name="list_drops")


def build_list_runs(deps: ToolDeps) -> BaseTool:
    def list_runs(sponsor_id: str | None = None) -> str:
        """List recon workbench runs with status and age in hours. Optionally for one sponsor."""
        if deps.recon is None:
            return fail(NOT_CONFIGURED)
        try:
            runs = deps.recon.runs(sponsor_id)
        except ReconError as exc:
            return fail(str(exc))
        return ok(
            runs=[
                {
                    "run_id": _run_id(run),
                    "sponsor_id": run.get("sponsor_id"),
                    "status": run.get("status"),
                    "upload_name": run.get("upload_name"),
                    "age_hours": _hours_since(run.get("created_at")),
                    "updated_hours": _hours_since(run.get("updated_at")),
                }
                for run in runs
            ]
        )

    return StructuredTool.from_function(list_runs, name="list_runs")


def build_get_run(deps: ToolDeps) -> BaseTool:
    def get_run(run_id: str) -> str:
        """Show one run: phase, status, the gate it waits at with its message and blocked
        reasons, and any brief questions. Read-only; gates are answered by a human in the workbench."""
        if deps.recon is None:
            return fail(NOT_CONFIGURED)
        try:
            run = deps.recon.run(run_id)
        except ReconError as exc:
            return fail(str(exc))
        pending = run.get("pending")
        pending = pending if isinstance(pending, dict) else {}
        brief = run.get("brief")
        questions = (brief.get("questions") if isinstance(brief, dict) else None) or []
        return ok(
            run_id=_run_id(run) or run_id,
            sponsor_id=run.get("sponsor_id"),
            phase=run.get("phase"),
            status=run.get("status"),
            error=run.get("error"),
            working=run.get("working"),
            age_hours=_hours_since(run.get("created_at")),
            gate=pending.get("gate"),
            gate_message=pending.get("message"),
            blocked_reasons=pending.get("blocked_reasons"),
            brief_questions=[
                {"id": q.get("id"), "text": q.get("text"), "options": q.get("options")}
                for q in questions
                if isinstance(q, dict)
            ],
        )

    return StructuredTool.from_function(get_run, name="get_run")


def build_start_run(deps: ToolDeps) -> BaseTool:
    def start_run(sponsor_id: str, file_name: str, sha256: str) -> str:
        """Upload one drop file to the recon workbench and start a run. Pass the sha256 that
        list_drops reported for the file; the upload is refused if the file has changed since."""
        if deps.recon is None or deps.drop_root is None:
            return fail(NOT_CONFIGURED)
        if not SPONSOR_ID.fullmatch(sponsor_id):
            return fail("invalid sponsor id")
        if not file_name or file_name != Path(file_name).name or file_name.startswith("."):
            return fail("invalid file name")
        folder = deps.drop_root / sponsor_id
        path = folder / file_name
        # Comparing against the unresolved root also refuses a symlinked sponsor folder.
        if path.is_symlink() or path.resolve().parent != deps.drop_root.resolve() / sponsor_id:
            return fail("the file is outside the sponsor folder")
        if not path.is_file():
            return fail("file not found")
        reason = _size_reason(file_name, path.stat().st_size)
        if reason is not None:
            return fail(reason)
        try:
            data = _read_no_follow(path)
        except OSError:
            return fail("the file could not be read")
        if hashlib.sha256(data).hexdigest() != sha256:
            return fail("the file changed since it was approved")
        try:
            existing = next((r for r in deps.recon.runs() if r.get("upload_sha") == sha256), None)
            if existing is not None:
                return fail("a run already exists for this file", run_id=_run_id(existing))
            run_id = deps.recon.start(sponsor_id, file_name, data)
        except ReconError as exc:
            return fail(str(exc))
        return ok(run_id=run_id)

    return StructuredTool.from_function(start_run, name="start_run")


def build_recon_tools(deps: ToolDeps) -> list[BaseTool]:
    return [
        build_list_sponsors(deps),
        build_list_drops(deps),
        build_list_runs(deps),
        build_get_run(deps),
        build_start_run(deps),
    ]
