"""Exercise the installed SDK session seam without a gateway."""

from __future__ import annotations

import base64
import io
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openshell import ExecResult, SandboxError
from openshell._proto import sandbox_pb2

from dot.assembly import GraphRuntime
from dot.config import Settings
from dot.persistence.db import Dot, MemoryRepositories, User
from dot.sandbox.base import SandboxMounts
from dot.sandbox.openshell_backend import OpenShellSandbox, build_input_image, load_policy, policy_for_profile


class Client:
    def __init__(self) -> None:
        self.creations: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.result = ExecResult(0, "hello\n", "")
        self.failure: Exception | None = None

    def create(self, **kwargs: Any) -> Any:
        self.creations.append(kwargs)
        return SimpleNamespace(id="sandbox-1", name=kwargs["name"], workspace="default")

    def wait_ready(self, name: str, **kwargs: Any) -> Any:
        if self.failure:
            raise self.failure
        return SimpleNamespace(id="sandbox-1", name=name, workspace="default")

    def exec(self, name: str, command: list[str], **kwargs: Any) -> ExecResult:
        self.calls.append({"command": command, **kwargs})
        if self.failure:
            raise self.failure
        return self.result

    def delete(self, name: str, **kwargs: Any) -> None:
        self.deleted.append(name)


def make(tmp_path: Path, client: Client, repos: MemoryRepositories) -> OpenShellSandbox:
    repos.create_user(User("local", "Local"))
    repos.create_dot(Dot("dot-1", "local", "research-analyst", "0", "thread-1", "active", datetime.now(UTC)))
    for name in ("in", "skills"):
        (tmp_path / name).mkdir(exist_ok=True)
    return OpenShellSandbox(
        "dot-1",
        SandboxMounts(tmp_path / "in", tmp_path / "skills"),
        gateway="local",
        audit=repos.append_audit,
        client=client,
        image_builder=lambda _image, _mounts: "sealed:123",
        object_root=tmp_path,
    )


def test_policy_is_explicit_and_readonly() -> None:
    default, research = load_policy("default"), load_policy("research")
    assert not default.network_policies
    assert set(default.filesystem.read_write) == {"/work", "/dev/null"}
    assert {"/in", "/skills"} <= set(default.filesystem.read_only)
    assert default.landlock.compatibility == "hard_requirement"
    for endpoint in research.network_policies["research"].endpoints:
        assert endpoint.access == sandbox_pb2.NETWORK_ACCESS_PRESET_READ_ONLY
        assert endpoint.enforcement == sandbox_pb2.NETWORK_ENFORCEMENT_MODE_ENFORCE
        # TLS mode is left unset: the gateway terminates automatically and rejects explicit values.
        assert endpoint.tls == sandbox_pb2.NETWORK_TLS_MODE_UNSPECIFIED and endpoint.protocol == "rest"
    for policy in (default, research):
        assert policy.process.run_as_user == policy.process.run_as_group == "1000"
    assert policy_for_profile("research") == "research"
    assert policy_for_profile("sweep") == "default"
    assert policy_for_profile("../../research") == "default"
    with pytest.raises(ValueError, match="unknown"):
        load_policy("../../research")


def test_lifecycle_execution_transfers_and_audit(tmp_path: Path) -> None:
    client, repos = Client(), MemoryRepositories()
    sandbox = make(tmp_path, client, repos)
    assert not client.creations
    with sandbox:
        sandbox.start()
        assert len(client.creations) == 1
        spec = client.creations[0]["spec"]
        assert spec.template.image == "sealed:123"
        name = client.creations[0]["name"]
        assert name.startswith("dot-") and len(name) <= 19  # the gateway's limit
        assert not spec.providers and not spec.environment
        assert spec.policy == load_policy("default")
        assert sandbox.execute("echo hello", timeout=7).output == "hello\n"
        assert client.calls[-1]["timeout_seconds"] == 7
        assert client.calls[-1]["no_login_shell"] is True
        assert client.calls[-1]["workdir"] == "/work"
        ok, denied, escaped = sandbox.upload_files(
            [
                ("/work/quoted 'file'", b"\x00\xff"),
                ("/in/new", b"no"),
                ("/work/../skills/new", b"no"),
            ]
        )
        assert ok.error is None and denied.error == escaped.error == "permission_denied"
        assert client.calls[-1]["stdin"] == b"\x00\xff"
        client.result = ExecResult(0, base64.b64encode(b"\x00\xff").decode(), "")
        assert sandbox.download_files(["/work/quoted 'file'"])[0].content == b"\x00\xff"
        client.result = ExecResult(3, "", "")
        assert sandbox.download_files(["/work"])[0].error == "is_directory"
        client.result = ExecResult(4, "", "")
        assert sandbox.download_files(["/work/missing"])[0].error == "file_not_found"
        client.result = ExecResult(56, "", "OpenShell policy denied: no matching network policy")
        response = sandbox.execute("curl https://blocked.example")
        assert response.exit_code == 56 and "policy denied" in response.output
        event = list(repos.audit.values())[-1]
        assert event.dot_id == "dot-1" and event.decision == "block"
        assert event.kind == "sandbox_denial" and "no matching" in event.detail["error"]
        client.result = ExecResult(0, '{"error":"policy_denied"}', "")
        sandbox.execute("curl -X POST https://api.github.com/zen")
        assert list(repos.audit.values())[-1].decision == "block"
    assert len(client.deleted) == 1
    with pytest.raises(RuntimeError, match="not started"):
        sandbox.execute("true")


def test_failures_cleanup_and_propagate(tmp_path: Path) -> None:
    client, repos = Client(), MemoryRepositories()
    sandbox = make(tmp_path, client, repos)
    client.failure = SandboxError("policy rejected")
    with pytest.raises(SandboxError, match="policy rejected"):
        sandbox.start()
    assert len(client.deleted) == 1
    client.failure = None
    sandbox.start()
    client.failure = SandboxError("network policy denied")
    with pytest.raises(SandboxError, match="network policy denied"):
        sandbox.execute("curl https://blocked.example")
    assert list(repos.audit.values())[-1].decision == "block"
    sandbox.close()


def test_idle_snapshot_and_restore(tmp_path: Path) -> None:
    client, repos = Client(), MemoryRepositories()
    sandbox = make(tmp_path, client, repos)
    sandbox.start()
    assert not sandbox.suspend_if_idle()
    client.result = ExecResult(0, base64.b64encode(b"work-archive").decode(), "")
    assert sandbox.suspend_if_idle(datetime.now(UTC) + timedelta(seconds=601))
    assert sandbox.snapshot_path().read_bytes() == b"work-archive"
    sandbox.start()
    assert any(call.get("stdin") == b"work-archive" for call in client.calls)
    assert any("tar -xf" in call["command"][-1] for call in client.calls)
    sandbox.close()


def test_image_copies_files_and_rejects_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mounts = SandboxMounts(tmp_path / "in", tmp_path / "skills")
    mounts.input_dir.mkdir()
    mounts.skills_dir.mkdir()
    (mounts.input_dir / "data.csv").write_bytes(b"value\n2\n")
    (mounts.skills_dir / "method.md").write_bytes(b"method")
    archives: list[bytes] = []

    class Images:
        def build(self, **kwargs: Any) -> None:
            assert kwargs["network_mode"] == "none"
            archives.append(kwargs["fileobj"].read())

    monkeypatch.setattr("docker.from_env", lambda: SimpleNamespace(images=Images(), close=lambda: None))
    image = build_input_image("dot-sandbox", mounts)
    assert build_input_image("dot-sandbox", mounts) == image
    with tarfile.open(fileobj=io.BytesIO(archives[0])) as archive:
        assert archive.extractfile("in/data.csv").read() == b"value\n2\n"
        assert archive.extractfile("skills/method.md").read() == b"method"
        assert b"chmod -R a-w /in /skills" in archive.extractfile("Dockerfile").read()
    (mounts.input_dir / "secret-link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        build_input_image("dot-sandbox", mounts)


def test_runtime_caches_per_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.store.memory import InMemoryStore

    settings = Settings(
        _env_file=None, sandbox_backend="openshell", openshell_gateway="local", object_root=str(tmp_path)
    )
    runtime = GraphRuntime(MemorySaver(), InMemoryStore(), audit_repositories=MemoryRepositories())
    created: list[Any] = []

    def factory(*args: Any, **kwargs: Any) -> Any:
        instance = SimpleNamespace(start=lambda: None, close=lambda: None)
        created.append(instance)
        return instance

    monkeypatch.setattr("dot.assembly.openshell_sandbox_for", factory)
    first = runtime.sandbox("dot-1", "chat", settings)
    assert runtime.sandbox("dot-1", "digest", settings) is first
    assert runtime.sandbox("dot-1", "research", settings) is not first
    assert len(created) == 2
    runtime.close()
