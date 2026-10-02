from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from dot.assembly import GraphRuntime, _tool_deps, build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.packs.loader import REPO_ROOT, load_pack, seed_store
from dot.persistence.db import Approval, Dot, MemoryRepositories, User
from dot.tools.artifacts import ArtifactStore
from dot.tools.effects import Effect
from dot.tools.native import recon
from dot.tools.native.deps import ToolDeps
from dot.tools.native.recon_client import ReconClient
from dot.tools.registry import builtin_registry
from tests.support.fake_recon import FakeRecon
from tests.support.scripted_model import ScriptedChatModel, say

ROWS = b"affiliate_id,name\n1,Alpha\n2,Beta\n"
SHA = hashlib.sha256(ROWS).hexdigest()
OTHER = b"affiliate_id\n9\n"
OTHER_SHA = hashlib.sha256(OTHER).hexdigest()
TOOLS = ("list_sponsors", "list_drops", "list_runs", "get_run", "start_run")

_fakes: list[FakeRecon] = []


@pytest.fixture(autouse=True)
def _no_gate_requests() -> Iterator[None]:
    yield
    for fake in _fakes:
        assert not [path for path in fake.paths if "/gate" in path]
    _fakes.clear()


@pytest.fixture
def fake() -> FakeRecon:
    workbench = FakeRecon([{"id": "sponsor-a", "name": "Sponsor A"}, {"id": "sponsor-b", "name": "Sponsor B"}])
    _fakes.append(workbench)
    return workbench


def _deps(tmp_path: Path, fake: FakeRecon, declined: frozenset[tuple[str, str, str]] = frozenset()) -> ToolDeps:
    drops = tmp_path / "drops"
    drops.mkdir(exist_ok=True)
    return ToolDeps(
        ArtifactStore(tmp_path / "artifacts"), recon=fake.client(), drop_root=drops, recon_declined=lambda: declined
    )


def _call(deps: ToolDeps, name: str, **args: Any) -> dict[str, Any]:
    tool = {tool.name: tool for tool in recon.build_recon_tools(deps)}[name]
    result: dict[str, Any] = json.loads(tool.invoke(args))
    return result


def _drop(deps: ToolDeps, sponsor_id: str, name: str, data: bytes = ROWS) -> Path:
    assert deps.drop_root is not None
    folder = deps.drop_root / sponsor_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def test_effects_are_read_and_start_run_is_write() -> None:
    registry = builtin_registry()
    assert [registry.effect(name) for name in TOOLS] == [Effect.read] * 4 + [Effect.write]


def test_every_tool_says_not_configured_without_deps(tmp_path: Path) -> None:
    empty = ToolDeps(ArtifactStore(tmp_path))
    args = {"get_run": {"run_id": "run-1"}, "start_run": {"sponsor_id": "a", "file_name": "a.csv", "sha256": SHA}}
    for name in TOOLS:
        assert _call(empty, name, **args.get(name, {})) == {
            "ok": False,
            "error": "the recon workbench is not configured",
        }


def test_drop_tools_need_a_drop_root(tmp_path: Path, fake: FakeRecon) -> None:
    deps = replace(_deps(tmp_path, fake), drop_root=None)
    assert _call(deps, "list_drops")["error"] == "the recon workbench is not configured"
    assert _call(deps, "list_sponsors") == {
        "ok": True,
        "sponsors": [{"id": "sponsor-a", "name": "Sponsor A"}, {"id": "sponsor-b", "name": "Sponsor B"}],
    }


def test_list_drops_reports_each_file(tmp_path: Path, fake: FakeRecon, monkeypatch: pytest.MonkeyPatch) -> None:
    deps = _deps(tmp_path, fake, frozenset({("sponsor-b", "declined.csv", OTHER_SHA)}))
    assert deps.drop_root is not None
    _drop(deps, "sponsor-a", "affiliates.csv")
    _drop(deps, "sponsor-a", "notes.pdf", b"%PDF")
    _drop(deps, "sponsor-a", "big.xlsx", b"x" * 64)
    _drop(deps, "sponsor-b", "declined.csv", OTHER)
    _drop(deps, "stranger", "who.csv", b"a\n")
    outside = tmp_path / "secret.csv"
    outside.write_bytes(b"secret\n")
    (deps.drop_root / "sponsor-a" / "link.csv").symlink_to(outside)
    (deps.drop_root / "sponsor-a" / "sub").mkdir()
    (deps.drop_root / "Not A Sponsor").mkdir()
    _drop(deps, "Not A Sponsor", "x.csv")
    run_id = fake.add_run("sponsor-a", "affiliates.csv", SHA)
    monkeypatch.setattr(recon, "MAX_BYTES", len(ROWS))

    result = _call(deps, "list_drops")

    assert result["ok"] is True
    files = {(f["sponsor_id"], f["file_name"]): f for f in result["files"]}
    assert list(files) == [
        ("sponsor-a", "affiliates.csv"),
        ("sponsor-a", "big.xlsx"),
        ("sponsor-a", "link.csv"),
        ("sponsor-a", "notes.pdf"),
        ("sponsor-b", "declined.csv"),
        ("stranger", "who.csv"),
    ]
    assert files["sponsor-a", "affiliates.csv"] == {
        "sponsor_id": "sponsor-a",
        "file_name": "affiliates.csv",
        "bytes": len(ROWS),
        "sha256": SHA,
        "supported": True,
        "reason": None,
        "run_id": run_id,
        "declined": False,
    }
    assert (files["sponsor-a", "notes.pdf"]["supported"], files["sponsor-a", "notes.pdf"]["reason"]) == (
        False,
        "unsupported type",
    )
    assert files["sponsor-a", "big.xlsx"]["reason"] == "too large"
    assert files["sponsor-a", "big.xlsx"]["supported"] is False
    link = files["sponsor-a", "link.csv"]
    assert (link["reason"], link["supported"], link["sha256"]) == ("symlink", False, None)
    assert files["stranger", "who.csv"]["reason"] == "unknown sponsor"
    assert files["stranger", "who.csv"]["supported"] is False
    declined = files["sponsor-b", "declined.csv"]
    assert (declined["declined"], declined["run_id"], declined["reason"]) == (True, None, None)


def test_list_drops_for_one_sponsor(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-a", "a.csv")
    _drop(deps, "sponsor-b", "b.tsv")
    files = _call(deps, "list_drops", sponsor_id="sponsor-b")["files"]
    assert [(f["sponsor_id"], f["file_name"]) for f in files] == [("sponsor-b", "b.tsv")]
    assert _call(deps, "list_drops", sponsor_id="missing")["files"] == []
    assert _call(deps, "list_drops", sponsor_id="../drops") == {"ok": False, "error": "invalid sponsor id"}


def test_list_drops_skips_a_symlinked_sponsor_folder(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    assert deps.drop_root is not None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.csv").write_bytes(ROWS)
    (deps.drop_root / "sponsor-a").symlink_to(elsewhere, target_is_directory=True)
    assert _call(deps, "list_drops")["files"] == []
    assert _call(deps, "list_drops", sponsor_id="sponsor-a")["files"] == []


def test_list_runs_is_compact(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    run_a = fake.add_run("sponsor-a", "a.csv", SHA)
    fake.add_run("sponsor-b", "b.csv", "f" * 64, status="locked")
    now = datetime.now(UTC)
    fake.runs[0]["created_at"] = (now - timedelta(hours=30, minutes=6)).isoformat()
    fake.runs[0]["updated_at"] = (now - timedelta(hours=2)).isoformat()

    everything = _call(deps, "list_runs")
    assert [r["run_id"] for r in everything["runs"]] == [run_a, "run-2"]
    first = everything["runs"][0]
    assert first == {
        "run_id": run_a,
        "sponsor_id": "sponsor-a",
        "status": "awaiting_brief",
        "upload_name": "a.csv",
        "age_hours": 30.1,
        "updated_hours": 2.0,
    }
    only_b = _call(deps, "list_runs", sponsor_id="sponsor-b")["runs"]
    assert [r["run_id"] for r in only_b] == ["run-2"]
    assert fake.requests[-1].url.params["sponsor_id"] == "sponsor-b"


def test_get_run_flattens_gate_and_brief(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    question = {"id": "q1", "text": "Which currency?", "options": ["USD", "EUR"], "extra": "dropped"}
    run_id = fake.add_run(
        "sponsor-a",
        "a.csv",
        SHA,
        phase="p1",
        pending={
            "gate": "brief",
            "message": "Answer the brief",
            "blocked_reasons": ["currency"],
            "allowed_actions": [],
        },
        brief={"questions": [question]},
        artifacts=[{"name": "big"}],
    )
    result = _call(deps, "get_run", run_id=run_id)
    assert result["ok"] is True
    assert {k: v for k, v in result.items() if k not in {"ok", "age_hours"}} == {
        "run_id": run_id,
        "sponsor_id": "sponsor-a",
        "phase": "p1",
        "status": "awaiting_brief",
        "error": None,
        "working": False,
        "gate": "brief",
        "gate_message": "Answer the brief",
        "blocked_reasons": ["currency"],
        "brief_questions": [{"id": "q1", "text": "Which currency?", "options": ["USD", "EUR"]}],
    }
    assert result["age_hours"] == 0.0

    idle = _call(deps, "get_run", run_id=fake.add_run("sponsor-a", "b.csv", "e" * 64))
    assert (idle["gate"], idle["gate_message"], idle["blocked_reasons"], idle["brief_questions"]) == (
        None,
        None,
        None,
        [],
    )


def test_get_run_refuses_a_bad_id_and_passes_workbench_errors(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    sent = len(fake.requests)
    assert _call(deps, "get_run", run_id="run-1/gate") == {"ok": False, "error": "invalid run id"}
    assert _call(deps, "get_run", run_id="../sponsors") == {"ok": False, "error": "invalid run id"}
    assert len(fake.requests) == sent
    assert _call(deps, "get_run", run_id="run-404") == {"ok": False, "error": "workbench returned 404: run not found"}


def test_unreachable_workbench_is_ok_false(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = ReconClient("http://recon.test", credentials=None, transport=httpx.MockTransport(down))
    (tmp_path / "sponsor-a").mkdir()
    (tmp_path / "sponsor-a" / "a.csv").write_bytes(ROWS)
    deps = ToolDeps(ArtifactStore(tmp_path / "artifacts"), recon=client, drop_root=tmp_path)
    args = {
        "get_run": {"run_id": "run-1"},
        "start_run": {"sponsor_id": "sponsor-a", "file_name": "a.csv", "sha256": SHA},
    }
    for name in TOOLS:
        assert _call(deps, name, **args.get(name, {})) == {"ok": False, "error": "workbench unreachable"}


def test_start_run_uploads_exactly_the_approved_bytes(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-a", "affiliates.csv")
    result = _call(deps, "start_run", sponsor_id="sponsor-a", file_name="affiliates.csv", sha256=SHA)
    assert result == {"ok": True, "run_id": "run-1"}
    assert fake.uploads == [{"sponsor_id": "sponsor-a", "file_name": "affiliates.csv", "data": ROWS}]
    assert fake.runs[0]["upload_sha"] == SHA and fake.runs[0]["status"] == "awaiting_brief"
    assert ("POST", "/runs") in [(r.method, r.url.path) for r in fake.requests]


@pytest.mark.parametrize(
    ("sponsor_id", "file_name", "error"),
    [
        ("sponsor-a", "../x.csv", "invalid file name"),
        ("sponsor-a", "a/b.csv", "invalid file name"),
        ("sponsor-a", ".hidden.csv", "invalid file name"),
        ("sponsor-a", "", "invalid file name"),
        ("Sponsor-A", "affiliates.csv", "invalid sponsor id"),
        ("../sponsor-a", "affiliates.csv", "invalid sponsor id"),
        ("-sponsor", "affiliates.csv", "invalid sponsor id"),
        ("sponsor-a", "missing.csv", "file not found"),
        ("sponsor-a", "notes.pdf", "unsupported type"),
    ],
)
def test_start_run_refuses_bad_names(
    tmp_path: Path, fake: FakeRecon, sponsor_id: str, file_name: str, error: str
) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-a", "affiliates.csv")
    _drop(deps, "sponsor-a", "notes.pdf")
    result = _call(deps, "start_run", sponsor_id=sponsor_id, file_name=file_name, sha256=SHA)
    assert result == {"ok": False, "error": error}
    assert fake.uploads == []


def test_start_run_refuses_a_symlink_out_of_the_folder(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    assert deps.drop_root is not None
    outside = tmp_path / "outside.csv"
    outside.write_bytes(ROWS)
    (deps.drop_root / "sponsor-a").mkdir()
    (deps.drop_root / "sponsor-a" / "link.csv").symlink_to(outside)
    result = _call(deps, "start_run", sponsor_id="sponsor-a", file_name="link.csv", sha256=SHA)
    assert result == {"ok": False, "error": "the file is outside the sponsor folder"}
    assert fake.uploads == []


def test_start_run_refuses_a_symlinked_sponsor_folder(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    assert deps.drop_root is not None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.csv").write_bytes(ROWS)
    (deps.drop_root / "sponsor-a").symlink_to(elsewhere, target_is_directory=True)
    result = _call(deps, "start_run", sponsor_id="sponsor-a", file_name="a.csv", sha256=SHA)
    assert result == {"ok": False, "error": "the file is outside the sponsor folder"}
    assert fake.uploads == []


def test_start_run_refuses_a_too_large_file(tmp_path: Path, fake: FakeRecon, monkeypatch: pytest.MonkeyPatch) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-a", "affiliates.csv")
    monkeypatch.setattr(recon, "MAX_BYTES", 8)
    result = _call(deps, "start_run", sponsor_id="sponsor-a", file_name="affiliates.csv", sha256=SHA)
    assert result == {"ok": False, "error": "too large"}
    assert fake.uploads == []


def test_start_run_refuses_a_file_changed_after_approval(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    path = _drop(deps, "sponsor-a", "affiliates.csv")
    path.write_bytes(ROWS + b"3,Gamma\n")
    result = _call(deps, "start_run", sponsor_id="sponsor-a", file_name="affiliates.csv", sha256=SHA)
    assert result == {"ok": False, "error": "the file changed since it was approved"}
    assert fake.uploads == []


def test_start_run_refuses_a_duplicate_upload(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-a", "copy.csv")
    existing = fake.add_run("sponsor-a", "affiliates.csv", SHA)
    result = _call(deps, "start_run", sponsor_id="sponsor-a", file_name="copy.csv", sha256=SHA)
    assert result == {"ok": False, "error": "a run already exists for this file", "run_id": existing}
    assert fake.uploads == []


def test_start_run_passes_a_workbench_refusal_through(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-c", "affiliates.csv")
    result = _call(deps, "start_run", sponsor_id="sponsor-c", file_name="affiliates.csv", sha256=SHA)
    assert result == {"ok": False, "error": "workbench returned 422: invalid sponsor or entity"}


def test_start_run_docstring_names_list_drops(tmp_path: Path) -> None:
    tools = {tool.name: tool for tool in recon.build_recon_tools(ToolDeps(ArtifactStore(tmp_path)))}
    assert set(tools) == set(TOOLS)
    assert "list_drops" in tools["start_run"].description and "sha256" in tools["start_run"].description


def test_assembly_wires_declined_from_rejected_start_run_cards(tmp_path: Path, fake: FakeRecon) -> None:
    repos = MemoryRepositories()
    repos.create_user(User("owner", "Owner"))
    now = datetime.now(UTC)
    repos.create_dot(Dot("dot-1", "owner", "research-analyst", "1", "thread-1", "active", now))
    repos.create_dot(Dot("dot-2", "owner", "research-analyst", "1", "thread-2", "active", now))
    args = {"sponsor_id": "sponsor-a", "file_name": "affiliates.csv", "sha256": SHA}
    cards = [
        Approval("a1", "dot-1", "{}", "start_run", args, "reject"),
        Approval("a2", "dot-1", "{}", "start_run", {**args, "file_name": "other.csv"}, "approve"),
        Approval("a3", "dot-1", "{}", "send_email", {**args, "file_name": "mail.csv"}, "reject"),
        Approval("a4", "dot-2", "{}", "start_run", {**args, "file_name": "theirs.csv"}, "reject"),
    ]
    for card in cards:
        repos.approvals[card.approval_id] = card
    runtime = GraphRuntime(MemorySaver(), InMemoryStore(), audit_repositories=repos)
    settings = Settings(_env_file=None, object_root=str(tmp_path))  # type: ignore[call-arg]
    dot = repos.get_dot("dot-1")
    base = ToolDeps(ArtifactStore(tmp_path), recon=fake.client(), drop_root=tmp_path)

    deps = _tool_deps(dot, settings, runtime, base)
    assert deps.recon_declined is not None
    assert deps.recon_declined() == frozenset({("sponsor-a", "affiliates.csv", SHA)})

    stub = _tool_deps(dot, settings, runtime, replace(base, recon_declined=lambda: frozenset({("x", "y", "z")})))
    assert stub.recon_declined is not None and stub.recon_declined() == frozenset({("x", "y", "z")})

    bare = _tool_deps(dot, settings, GraphRuntime(MemorySaver(), InMemoryStore()), base)
    assert bare.recon_declined is not None and bare.recon_declined() == frozenset()


@pytest.mark.parametrize("profile", ["chat", "sweep"])
def test_research_analyst_is_not_offered_recon_tools(tmp_path: Path, fake: FakeRecon, profile: str) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path / "objects"))  # type: ignore[call-arg]
    repos = MemoryRepositories()
    repos.create_user(User("u1", "Ada"))
    dot = Dot("dot-1", "u1", "research-analyst", "0", "thread-1", "active", datetime.now(UTC))
    repos.create_dot(dot)
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    seed_store(load_pack(REPO_ROOT / "packs/research-analyst"), runtime.store, dot.dot_id)
    model = ScriptedChatModel(script=[say("hi")])
    try:
        agent = build_dot_agent(
            dot, profile, settings=settings, runtime=runtime, model=model, deps=_deps(tmp_path, fake)
        )
        agent.invoke({"messages": [("user", "hi")]}, {"configurable": {"thread_id": f"probe-{profile}"}})
    finally:
        runtime.close()
    offered = set(model.offered[0])
    assert offered and not offered & set(TOOLS)


_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.mark.skipif(_ROOT, reason="root ignores file permissions")
def test_list_drops_reports_unreadable_entries_and_keeps_going(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    assert deps.drop_root is not None
    good = _drop(deps, "sponsor-a", "good.csv")
    locked = _drop(deps, "sponsor-a", "locked.csv")
    os.mkfifo(deps.drop_root / "sponsor-a" / "pipe.csv")
    _drop(deps, "sponsor-b", "b.csv")
    shut = deps.drop_root / "sponsor-b"
    locked.chmod(0)
    shut.chmod(0)
    try:
        result = _call(deps, "list_drops")
    finally:
        locked.chmod(0o600)
        shut.chmod(0o700)
    assert result["ok"] is True
    files = {(f["sponsor_id"], f["file_name"]): f for f in result["files"]}
    assert files["sponsor-a", "good.csv"]["supported"] is True
    assert files["sponsor-a", "good.csv"]["sha256"] == hashlib.sha256(good.read_bytes()).hexdigest()
    for key in [("sponsor-a", "locked.csv"), ("sponsor-a", "pipe.csv"), ("sponsor-b", None)]:
        assert (files[key]["supported"], files[key]["reason"], files[key]["sha256"]) == (False, "unreadable", None)


def test_list_drops_marks_hidden_files_unsupported(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-a", ".h.csv")
    (entry,) = _call(deps, "list_drops")["files"]
    assert (entry["file_name"], entry["supported"], entry["reason"]) == (".h.csv", False, "hidden")


def test_list_drops_survives_a_failing_declined_lookup(
    tmp_path: Path, fake: FakeRecon, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> frozenset[tuple[str, str, str]]:
        raise RuntimeError("connection to db-secret-host refused")

    deps = replace(_deps(tmp_path, fake), recon_declined=broken)
    _drop(deps, "sponsor-a", "a.csv")
    with caplog.at_level("WARNING"):
        raw = recon.build_list_drops(deps).invoke({})
    assert "db-secret-host" not in raw
    (entry,) = json.loads(raw)["files"]
    assert entry["declined"] is False and entry["supported"] is True
    assert "declined" in caplog.text


@pytest.mark.parametrize(("sponsor_id", "file_name"), [("sponsor-a", "a\x00.csv"), ("sponsor-a\x00", "a.csv")])
def test_start_run_refuses_a_nul_byte(tmp_path: Path, fake: FakeRecon, sponsor_id: str, file_name: str) -> None:
    deps = _deps(tmp_path, fake)
    _drop(deps, "sponsor-a", "a.csv")
    result = _call(deps, "start_run", sponsor_id=sponsor_id, file_name=file_name, sha256=SHA)
    assert result["ok"] is False and result["error"] in {"invalid file name", "invalid sponsor id"}
    assert fake.uploads == []


def test_start_run_refuses_a_fifo(tmp_path: Path, fake: FakeRecon) -> None:
    deps = _deps(tmp_path, fake)
    assert deps.drop_root is not None
    (deps.drop_root / "sponsor-a").mkdir()
    os.mkfifo(deps.drop_root / "sponsor-a" / "pipe.csv")
    result = _call(deps, "start_run", sponsor_id="sponsor-a", file_name="pipe.csv", sha256=SHA)
    assert result == {"ok": False, "error": "the file could not be read"}
