"""Execute the delegated scripted coder against a real sandbox."""

import json
from pathlib import Path

import pytest

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.persistence.db import MemoryRepositories, User
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


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("docker", marks=pytest.mark.docker),
        pytest.param("openshell", marks=pytest.mark.openshell),
    ],
)
def test_coder_computes_csv(backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_url=None, object_root=str(tmp_path), sandbox_backend=backend)
    dot = stage_task(settings)
    supervisor = ScriptedChatModel(
        script=[
            tools(call("task", subagent_type="coder", description=REQUEST)),
            say("Verified the CSV output."),
        ]
    )
    heavy = ScriptedChatModel(
        script=[
            tools(call("write_file", file_path=SCRIPT_PATH, content=SCRIPT)),
            tools(call("execute", command=f"python {SCRIPT_PATH}")),
            tools(call("read_file", file_path=OUTPUT_PATH)),
            say("Verified /work/result.json."),
        ]
    )
    monkeypatch.setattr("dot.assembly.chat_model", lambda role, _settings: heavy if role == "heavy" else supervisor)
    repos = MemoryRepositories()
    repos.create_user(User("local", "Local"))
    repos.create_dot(dot)
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    try:
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime)
        invoke_with_sandbox_approvals(agent, dot)
        sandbox = runtime.sandbox(dot.dot_id, "chat", settings)
        script, output = sandbox.download_files([SCRIPT_PATH, OUTPUT_PATH])
        assert script.error is None and script.content == SCRIPT.encode()
        assert output.error is None and json.loads(output.content) == EXPECTED
        assert heavy.calls == 4
    finally:
        runtime.close()
