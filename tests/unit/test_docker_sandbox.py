"""Offline checks of the Docker sandbox isolation contract."""

from __future__ import annotations

import base64
import tarfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from dot.config import Settings
from dot.sandbox.base import SandboxMounts
from dot.sandbox.docker_backend import DockerSandbox, docker_sandbox_for


def _mounts(tmp_path: Path) -> SandboxMounts:
    for name in ("in", "skills"):
        (tmp_path / name).mkdir()
    return SandboxMounts(tmp_path / "in", tmp_path / "skills")


def test_docker_run_kwargs_isolate(tmp_path: Path) -> None:
    kwargs = DockerSandbox("dot-1", _mounts(tmp_path)).run_kwargs()
    assert kwargs["image"] == "dot-sandbox"
    assert kwargs["network_mode"] == "none"
    assert kwargs["read_only"] is True
    assert kwargs["environment"] == {}
    assert kwargs["cap_drop"] == ["ALL"]
    assert "/work" in kwargs["tmpfs"]
    modes = {v["bind"]: v["mode"] for v in kwargs["volumes"].values()}
    assert modes == {"/in": "ro", "/skills": "ro"}
    assert kwargs["labels"] == {"dot.sandbox": "dot-1"}
    assert kwargs["name"] == "dot-sandbox-dot-1"
    assert kwargs["mem_limit"] == "1g"


class _FakeContainer:
    id = "abc123"

    def __init__(self) -> None:
        self.removed = False
        self.commands: list[list[str]] = []

    def exec_run(self, cmd: list[str], workdir: str | None = None, demux: bool = False) -> Any:
        self.commands.append(cmd)

        class Result:
            exit_code = 0
            output = (b"", b"") if demux else b"hello\n"

        return Result()

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeClient:
    def __init__(self, container: _FakeContainer | None = None) -> None:
        self.container = container or _FakeContainer()
        self.kwargs: dict[str, object] = {}
        outer = self

        class Containers:
            def list(self, all: bool, filters: dict[str, str]) -> list[object]:
                return []

            def run(self, **kwargs: object) -> _FakeContainer:
                outer.kwargs = kwargs
                outer.container.removed = False
                return outer.container

        self.containers = Containers()


def test_docker_lifecycle_and_upload_guard(tmp_path: Path) -> None:
    client = _FakeClient()
    with DockerSandbox("dot-1", _mounts(tmp_path), client=client, object_root=tmp_path / "objects") as sandbox:
        assert sandbox.id == "abc123"
        assert sandbox.execute("echo hello").output == "hello\n"
        ok, denied, escaped = sandbox.upload_files(
            [("/work/recipe.py", b"x"), ("/in/evil.csv", b"x"), ("/work/../../etc/passwd", b"x")]
        )
        assert ok.error is None
        assert denied.error == "permission_denied"
        assert escaped.error == "permission_denied"
        assert all("/in/" not in " ".join(command) for command in client.container.commands)
    assert client.container.removed


class _SnapshotContainer(_FakeContainer):
    def __init__(self, tar_b64: bytes) -> None:
        super().__init__()
        self.tar_b64 = tar_b64

    def exec_run(self, cmd: list[str], workdir: str | None = None, demux: bool = False) -> Any:
        self.commands.append(cmd)
        script = " ".join(cmd)
        payload = self.tar_b64 if "base64 -w 0" in script else b""

        class Result:
            exit_code = 0
            output = (payload, b"") if demux else payload

        return Result()


def _tar_bytes() -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        payload = b"kept-intact\n"
        info = tarfile.TarInfo(name="note.txt")
        info.size = len(payload)
        archive.addfile(info, BytesIO(payload))
    return buf.getvalue()


def test_suspend_if_idle_snapshots_and_restores_on_start(tmp_path: Path) -> None:
    archived = _tar_bytes()
    container = _SnapshotContainer(base64.b64encode(archived))
    client = _FakeClient(container)
    sandbox = DockerSandbox(
        "dot-1",
        _mounts(tmp_path),
        client=client,
        object_root=tmp_path / "objects",
        idle_s=600,
    )
    sandbox.start()
    sandbox.execute("echo hello")
    assert sandbox.suspend_if_idle(datetime.now(UTC)) is False
    assert sandbox.suspend_if_idle(datetime.now(UTC) + timedelta(seconds=600)) is True
    assert sandbox.snapshot_path().read_bytes() == archived
    assert container.removed
    sandbox.start()
    restored = " ".join(" ".join(command) for command in container.commands)
    assert "tar -C /work --no-same-owner -xf /tmp/dot-work.tar" in restored
    sandbox.close()


def test_suspend_keeps_the_container_when_the_snapshot_is_unreadable(tmp_path: Path) -> None:
    container = _SnapshotContainer(b"***")
    client = _FakeClient(container)
    sandbox = DockerSandbox("dot-1", _mounts(tmp_path), client=client, object_root=tmp_path / "objects")
    sandbox.start()
    with pytest.raises(RuntimeError, match="not valid base64"):
        sandbox.suspend()
    assert container.removed is False
    sandbox.close()


def test_docker_sandbox_for_prepares_dirs_without_starting(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, object_root=str(tmp_path))  # type: ignore[call-arg]

    class Boom:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(name)

    sandbox = docker_sandbox_for("dot-abc", settings, client=Boom())
    assert sandbox.id == "dot-dot-abc"
    assert (tmp_path / "dot-abc" / "sandbox" / "in").is_dir()
    assert (tmp_path / "dot-abc" / "sandbox" / "skills").is_dir()
    assert sandbox.snapshot_path() == tmp_path / "dot-abc" / "sandbox" / "work.tar"
