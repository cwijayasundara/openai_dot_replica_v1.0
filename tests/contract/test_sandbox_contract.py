"""The same filesystem and execution contract against either real backend."""

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from dot.config import Settings
from dot.persistence.db import Dot, MemoryRepositories, User
from dot.sandbox.base import SandboxMounts
from dot.sandbox.docker_backend import DockerSandbox
from dot.sandbox.openshell_backend import OpenShellSandbox


def repositories(dot_id: str) -> MemoryRepositories:
    repos = MemoryRepositories()
    repos.create_user(User("local", "Local"))
    repos.create_dot(Dot(dot_id, "local", "research-analyst", "0", dot_id, "active", datetime.now(UTC)))
    return repos


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("docker", marks=pytest.mark.docker),
        pytest.param("openshell", marks=pytest.mark.openshell),
    ],
)
def test_sandbox_contract(backend: str, tmp_path: Path) -> None:
    settings = Settings()
    for name in ("in", "skills"):
        (tmp_path / name).mkdir()
    (tmp_path / "in" / "input.txt").write_text("input")
    (tmp_path / "skills" / "method.md").write_text("method")
    mounts = SandboxMounts(tmp_path / "in", tmp_path / "skills")
    dot_id = f"x2-{uuid4().hex}"
    if backend == "docker":
        sandbox = DockerSandbox(dot_id, mounts, image=settings.sandbox_image, object_root=tmp_path)
    else:
        assert settings.openshell_gateway, "set DOT_OPENSHELL_GATEWAY to a registered gateway name"
        sandbox = OpenShellSandbox(
            dot_id,
            mounts,
            gateway=settings.openshell_gateway,
            image=settings.sandbox_image,
            audit=repositories(dot_id).append_audit,
            object_root=tmp_path,
        )
    with sandbox:
        assert sandbox.execute("pwd").output.strip() == "/work"
        assert sandbox.execute("cat /in/input.txt /skills/method.md").output == "inputmethod"
        for path in ("/in/input.txt", "/skills/method.md", "/etc/dot-test"):
            assert sandbox.execute(f"echo bad > {path}").exit_code != 0
        assert sandbox.upload_files([("/work/nested/data.bin", b"\x00\xff")])[0].error is None
        assert sandbox.download_files(["/work/nested/data.bin"])[0].content == b"\x00\xff"
        assert sandbox.write("/work/note.txt", "hello world").error is None
        read = sandbox.read("/work/note.txt")
        assert read.error is None and "hello world" in read.file_data["content"]
        assert sandbox.edit("/work/note.txt", "hello", "goodbye").error is None
        read = sandbox.read("/work/note.txt")
        assert read.error is None and "goodbye world" in read.file_data["content"]
        assert sandbox.execute("sleep 10", timeout=1).exit_code != 0


@pytest.mark.openshell
def test_research_egress_denial_is_audited(tmp_path: Path) -> None:
    settings = Settings()
    assert settings.openshell_gateway, "set DOT_OPENSHELL_GATEWAY"
    for name in ("in", "skills"):
        (tmp_path / name).mkdir()
    dot_id = f"x2-{uuid4().hex}"
    repos = repositories(dot_id)
    with OpenShellSandbox(
        dot_id,
        SandboxMounts(tmp_path / "in", tmp_path / "skills"),
        gateway=settings.openshell_gateway,
        image=settings.sandbox_image,
        policy="research",
        audit=repos.append_audit,
        object_root=tmp_path,
    ) as sandbox:
        allowed = sandbox.execute("curl -fsS --max-time 20 https://api.github.com/zen")
        assert allowed.exit_code == 0, allowed.output
        # OpenShell 0.1.2 refuses a host outside the policy at connect time, before
        # its proxy: no descriptive body and no gateway log. The failed command is
        # still audited.
        before = len(repos.audit)
        denied = sandbox.execute("curl -sS --max-time 20 https://example.com")
        assert denied.exit_code != 0
        assert "could not connect" in denied.output.lower() or "denied" in denied.output.lower(), denied.output
        assert len(repos.audit) == before + 1
        assert list(repos.audit.values())[-1].detail["policy"] == "research"
        # The descriptive denial is in OpenShell's OCSF sandbox log, pushed asynchronously.
        assert _ocsf_denial(sandbox, "example.com"), "OpenShell logged no denial for example.com"
        # A method the policy does not allow on an allowed host gets the proxy's
        # descriptive denial, recorded as a block.
        write = sandbox.execute("curl -sS --max-time 20 -X POST https://api.github.com/zen")
        assert "policy_denied" in write.output and "not permitted by policy" in write.output, write.output
        event = list(repos.audit.values())[-1]
        assert event.decision == "block" and event.kind == "sandbox_denial"


def _ocsf_denial(sandbox: OpenShellSandbox, host: str, wait_s: float = 20.0) -> bool:
    name = sandbox._name
    assert name is not None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        logs = subprocess.run(["openshell", "logs", name], capture_output=True, text=True, timeout=30).stdout
        if any("DENIED" in line and host in line for line in logs.splitlines() if "NET:" in line):
            return True
        time.sleep(1)
    return False
