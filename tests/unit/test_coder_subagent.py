"""Coder role, tool boundaries and delegation, with no model or shell execution."""

import json
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileData,
    FileDownloadResponse,
    FileUploadResponse,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import create_file_data
from langchain_core.messages import HumanMessage

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.middleware.guard import FS_TOOLS
from dot.packs.loader import REPO_ROOT, load_pack, memories_namespace, seed_store
from dot.sandbox.base import RunSandbox
from tests.support.coder_task import (
    EXPECTED,
    OUTPUT_PATH,
    REQUEST,
    SCRIPT,
    SCRIPT_PATH,
    invoke_with_sandbox_approvals,
    stage_task,
)
from tests.support.scripted_model import ScriptedChatModel, call, say, tools


class RecordingSandbox(RunSandbox):
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    @property
    def id(self) -> str:
        return "recording"

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        assert command == f"python {SCRIPT_PATH}"
        assert self.files[SCRIPT_PATH] == SCRIPT
        self.commands.append(command)
        self.files[OUTPUT_PATH] = json.dumps(EXPECTED)
        return ExecuteResponse(output=json.dumps(EXPECTED), exit_code=0, truncated=False)

    def write(self, file_path: str, content: str) -> WriteResult:
        if not file_path.startswith("/work/"):
            return WriteResult(error="permission_denied")
        self.files[file_path] = content
        return WriteResult(path=file_path)

    def read(self, file_path: str, **kwargs: Any) -> ReadResult:
        content = self.files.get(file_path)
        if content is None:
            return ReadResult(error="file_not_found")
        return ReadResult(file_data=FileData(content=content, encoding="utf-8"))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        raise AssertionError("unexpected upload")

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        raise AssertionError("unexpected download")


def test_coder_uses_heavy_model_and_sandbox_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    supervisor = ScriptedChatModel(
        script=[
            tools(call("task", subagent_type="coder", description=REQUEST)),
            say("Complete: /work/result.json"),
        ]
    )
    heavy = ScriptedChatModel(
        script=[
            tools(call("write_file", file_path="/memories/AGENTS.md", content="overwritten")),
            tools(
                call("send_email", to="x@example.com", subject="no", body="no"),
                call("web_search", query="no"),
                call("task", subagent_type="general-purpose", description="no"),
            ),
            tools(call("write_file", file_path=SCRIPT_PATH, content=SCRIPT)),
            tools(call("execute", command=f"python {SCRIPT_PATH}")),
            tools(call("read_file", file_path=OUTPUT_PATH)),
            say("Verified /work/compute.py and /work/result.json; total value is 33.00."),
        ]
    )
    roles: list[str] = []

    def model(role: str, _settings: Settings) -> ScriptedChatModel:
        roles.append(role)
        return heavy if role == "heavy" else supervisor

    monkeypatch.setattr("dot.assembly.chat_model", model)
    sandbox = RecordingSandbox()
    runtime = build_graph_runtime(settings)
    seed_store(load_pack(REPO_ROOT / "packs" / dot.pack_name), runtime.store, dot.dot_id)
    runtime.store.put(memories_namespace(dot.dot_id), "/AGENTS.md", dict(create_file_data("Keep this memory.")))
    original = runtime.store.get(memories_namespace(dot.dot_id), "/AGENTS.md")
    used: list[str] = []

    def factory(dot_id: str) -> RunSandbox:
        used.append(dot_id)
        return sandbox

    agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, sandbox_factory=factory)
    assert not used
    try:
        result = invoke_with_sandbox_approvals(agent, dot)
        assert runtime.store.get(memories_namespace(dot.dot_id), "/AGENTS.md") == original
    finally:
        runtime.close()
    assert "heavy" in roles and heavy.calls == 6 and supervisor.calls == 2
    assert all(set(names) == FS_TOOLS for names in heavy.offered)
    assert all(set(names).isdisjoint(FS_TOOLS) for names in supervisor.offered)
    assert sandbox.commands == [f"python {SCRIPT_PATH}"]
    assert json.loads(sandbox.files[OUTPUT_PATH]) == EXPECTED
    assert set(sandbox.files) == {SCRIPT_PATH, OUTPUT_PATH}
    assert "result.json" in result["messages"][-1].content
    assert "read-only" in str(heavy.seen[0][0].content)
    tool_results = [str(message.content) for request in heavy.seen for message in request if message.type == "tool"]
    assert any("permission_denied" in content for content in tool_results)
    assert any("not allowed" in content or "not a valid tool" in content for content in tool_results)


def test_sweep_cannot_delegate_to_coder(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_url=None, object_root=str(tmp_path))
    dot = stage_task(settings)
    model = ScriptedChatModel(script=[tools(call("task", subagent_type="coder", description=REQUEST)), say("Denied")])

    def forbidden(_dot_id: str) -> RunSandbox:
        raise AssertionError("sweep started a sandbox")

    runtime = build_graph_runtime(settings)
    try:
        agent = build_dot_agent(
            dot, "sweep", settings=settings, runtime=runtime, model=model, sandbox_factory=forbidden
        )
        result = agent.invoke({"messages": [HumanMessage(REQUEST)]}, {"configurable": {"thread_id": dot.thread_id}})
    finally:
        runtime.close()
    assert any("not allowed" in str(message.content) for message in result["messages"] if message.type == "tool")
