"""Recon workbench tools: read drops and runs, and start a run on an approved file.

No tool here reaches a workbench gate; the client has no method for it. File
contents never reach the model, only names, sizes and hashes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
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
SPONSORS_PAGE = "/wiki/sponsors.md"
# Workbench messages and errors are clipped to this many characters.
MAX_TEXT = 300
_CHUNK = 1024 * 1024
# O_NOFOLLOW refuses a symlink swapped in after the is_symlink check; O_NONBLOCK keeps a
# FIFO from hanging the open. Only regular files are read.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

log = logging.getLogger(__name__)


def _open_regular(path: Path) -> tuple[int, int]:
    """An fd and size for a regular file. Raises OSError for anything else."""
    fd = os.open(path, _OPEN_FLAGS)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise OSError(f"not a regular file: {path.name}")
    return fd, info.st_size


def _sha256(path: Path) -> tuple[int, str]:
    fd, size = _open_regular(path)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return size, digest.hexdigest()


def _entry(sponsor_id: str, file_name: str | None, reason: str) -> dict[str, Any]:
    return {
        "sponsor_id": sponsor_id,
        "file_name": file_name,
        "bytes": None,
        "sha256": None,
        "supported": False,
        "reason": reason,
        "run_id": None,
        "declined": False,
    }


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


def _clip(value: Any) -> str | None:
    return None if value is None else str(value)[:MAX_TEXT]


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
    try:
        candidates = [root / sponsor_id] if sponsor_id else sorted(root.iterdir())
    except OSError:
        log.warning("recon drop root %s could not be listed", root)
        return []
    folders = []
    for path in candidates:
        try:
            if SPONSOR_ID.fullmatch(path.name) and stat.S_ISDIR(path.lstat().st_mode):
                folders.append(path)
        except OSError:
            continue
    return folders


def _declined(deps: ToolDeps) -> frozenset[tuple[str, str, str]]:
    if deps.recon_declined is None:
        return frozenset()
    try:
        return deps.recon_declined()
    except Exception:
        # The lookup reads the database; its error text never reaches the model.
        log.warning("recon declined-files lookup failed; treating none as declined", exc_info=True)
        return frozenset()


def _drop_entry(
    folder: Path, path: Path, known: set[str], by_sha: dict[str, str | None], declined: frozenset[tuple[str, str, str]]
) -> dict[str, Any] | None:
    sponsor_id = folder.name
    try:
        mode = path.lstat().st_mode
    except OSError:
        return _entry(sponsor_id, path.name, "unreadable")
    if stat.S_ISLNK(mode):
        return _entry(sponsor_id, path.name, "symlink")
    if stat.S_ISDIR(mode):
        return None
    try:
        size, sha = _sha256(path)
    except OSError:
        return _entry(sponsor_id, path.name, "unreadable")
    if path.name.startswith("."):
        reason: str | None = "hidden"
    elif sponsor_id not in known:
        reason = "unknown sponsor"
    else:
        reason = _size_reason(path.name, size)
    return {
        "sponsor_id": sponsor_id,
        "file_name": path.name,
        "bytes": size,
        "sha256": sha,
        "supported": reason is None,
        "reason": reason,
        "run_id": by_sha.get(sha),
        "declined": (sponsor_id, path.name, sha) in declined,
    }


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_contacts(page: str) -> dict[str, str]:
    """Sponsor id to contact from the first ``| id | ... | contact |`` table. Bad rows are skipped."""
    columns: list[str] | None = None
    contacts: dict[str, str] = {}
    for line in page.splitlines():
        if not line.strip().startswith("|"):
            if columns is not None:
                break
            continue
        cells = _cells(line)
        if columns is None:
            lowered = [cell.lower() for cell in cells]
            if "id" in lowered and "contact" in lowered:
                columns = lowered
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if len(cells) != len(columns):
            continue
        row = dict(zip(columns, cells, strict=True))
        if row["id"] and row["contact"]:
            contacts[row["id"]] = row["contact"]
    return contacts


def _contacts(deps: ToolDeps) -> dict[str, str]:
    if deps.wiki_page is None:
        return {}
    try:
        page = deps.wiki_page(SPONSORS_PAGE)
    except Exception:
        # The read goes to the dot's store; its error text never reaches the model.
        log.warning("reading %s failed; sponsors are listed without contacts", SPONSORS_PAGE, exc_info=True)
        return {}
    return parse_contacts(page) if page else {}


def build_list_sponsors(deps: ToolDeps) -> BaseTool:
    def list_sponsors() -> str:
        """List the sponsors registered in the recon workbench, as id, name and the contact
        address from /wiki/sponsors.md (null when the wiki has none)."""
        if deps.recon is None:
            return fail(NOT_CONFIGURED)
        try:
            sponsors = deps.recon.sponsors()
        except ReconError as exc:
            return fail(str(exc))
        contacts = _contacts(deps)
        return ok(
            sponsors=[
                {"id": s.get("id"), "name": s.get("name"), "contact": contacts.get(str(s.get("id")))} for s in sponsors
            ]
        )

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
        declined = _declined(deps)
        files: list[dict[str, Any]] = []
        for folder in _sponsor_dirs(deps.drop_root, sponsor_id):
            try:
                paths = sorted(folder.iterdir())
            except OSError:
                # One entry for the folder, so the model can say it could not be read.
                files.append(_entry(folder.name, None, "unreadable"))
                continue
            for path in paths:
                entry = _drop_entry(folder, path, known, by_sha, declined)
                if entry is not None:
                    files.append(entry)
        return ok(files=files)

    return StructuredTool.from_function(list_drops, name="list_drops")


def build_list_runs(deps: ToolDeps) -> BaseTool:
    def list_runs(sponsor_id: str | None = None) -> str:
        """List recon workbench runs with age in hours. Optionally for one sponsor. The status is the
        record's: "scoping" until the run is rejected or locked. Call get_run for where a run stands."""
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
                }
                for run in runs
            ]
        )

    return StructuredTool.from_function(list_runs, name="list_runs")


def build_get_run(deps: ToolDeps) -> BaseTool:
    def get_run(run_id: str) -> str:
        """Show where one run stands: phase, live status, age in hours, the gate it waits at with its
        message and blocked reasons, any brief questions, and any error or failed background job.
        Read-only; gates are answered by a human in the workbench."""
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
        # The workbench nests the run record, which holds created_at, beside the graph state.
        record = run.get("record")
        record = record if isinstance(record, dict) else {}
        return ok(
            run_id=_run_id(run) or run_id,
            sponsor_id=run.get("sponsor_id", record.get("sponsor_id")),
            phase=run.get("phase"),
            status=run.get("status"),
            error=_clip(run.get("error")),
            job_error=_clip(run.get("job_error")),
            working=run.get("working"),
            age_hours=_hours_since(run.get("created_at") or record.get("created_at")),
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
        if not file_name or "\x00" in file_name or file_name != Path(file_name).name or file_name.startswith("."):
            return fail("invalid file name")
        if Path(file_name).suffix.lower() not in SUPPORTED:
            return fail("unsupported type")
        path = deps.drop_root / sponsor_id / file_name
        try:
            # Comparing against the resolved root also refuses a symlinked sponsor folder.
            if path.is_symlink() or path.resolve().parent != deps.drop_root.resolve() / sponsor_id:
                return fail("the file is outside the sponsor folder")
            fd, size = _open_regular(path)
        except FileNotFoundError:
            return fail("file not found")
        except OSError:
            return fail("the file could not be read")
        with os.fdopen(fd, "rb") as handle:
            if size > MAX_BYTES:
                return fail("too large")
            try:
                data = handle.read()
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
