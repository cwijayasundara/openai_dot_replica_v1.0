"""Live X3 acceptance through the supervisor and heavy coder on both backends."""

import json
from pathlib import Path

import pytest

from dot.assembly import build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.persistence.db import MemoryRepositories, User
from tests.support.coder_task import (
    EXPECTED,
    OUTPUT_PATH,
    SCRIPT_PATH,
    invoke_with_sandbox_approvals,
    stage_task,
)

pytestmark = pytest.mark.live


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("docker", marks=pytest.mark.docker),
        pytest.param("openshell", marks=pytest.mark.openshell),
    ],
)
def test_live_coder_writes_and_runs_csv_script(backend: str, tmp_path: Path) -> None:
    settings = Settings(database_url=None, object_root=str(tmp_path), sandbox_backend=backend)
    assert settings.fireworks_api_key, "configure DOT_FIREWORKS_API_KEY before running live acceptance"
    dot = stage_task(settings)
    repos = MemoryRepositories()
    repos.create_user(User("local", "Local"))
    repos.create_dot(dot)
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    try:
        agent = build_dot_agent(dot, "chat", settings=settings, runtime=runtime)
        result = invoke_with_sandbox_approvals(agent, dot)
        delegated = [
            call
            for message in result["messages"]
            for call in getattr(message, "tool_calls", [])
            if call["name"] == "task" and call["args"].get("subagent_type") == "coder"
        ]
        assert delegated, "supervisor did not delegate to coder"
        sandbox = runtime.sandbox(dot.dot_id, "chat", settings)
        script, output = sandbox.download_files([SCRIPT_PATH, OUTPUT_PATH])
        assert script.error is None and script.content
        assert output.error is None and json.loads(output.content) == EXPECTED
        # Re-run the actual delivered script to prove the result is reproducible.
        rerun = sandbox.execute(f"python {SCRIPT_PATH}")
        assert rerun.exit_code == 0, rerun.output
        assert json.loads(sandbox.download_files([OUTPUT_PATH])[0].content) == EXPECTED
    finally:
        runtime.close()
