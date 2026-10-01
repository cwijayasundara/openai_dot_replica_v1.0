"""Docker sandbox: network none, read-only root, tmpfs /work, empty environment.

Idle suspend writes a tar of /work under the object root, then removes the
container. The next start restores that tar. /work is tmpfs, so removing the
container drops anything that was not snapshotted.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)

from dot.config import Settings

from .base import (
    DEFAULT_TIMEOUT_S,
    IN_DIR,
    SKILLS_DIR,
    WORK_DIR,
    RunSandbox,
    SandboxMounts,
    truncate,
)

CLEAN_ENV = "env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/work LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1"
UPLOAD_CHUNK = 64 * 1024
_SNAPSHOT_TAR = "/tmp/dot-work.tar"
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _chunks(text: str) -> list[str]:
    return [text[i : i + UPLOAD_CHUNK] for i in range(0, len(text), UPLOAD_CHUNK)] or [""]


def _container_name(dot_id: str) -> str:
    cleaned = _NAME_UNSAFE.sub("-", dot_id).strip("-.")
    return f"dot-sandbox-{cleaned[:180] or 'sandbox'}"


def _stdout(result: Any) -> bytes:
    output = result.output
    if isinstance(output, tuple):
        first = output[0]
        return first if isinstance(first, bytes) else b""
    return output if isinstance(output, bytes) else b""


def _stderr(result: Any) -> bytes:
    output = result.output
    if isinstance(output, tuple) and len(output) > 1 and isinstance(output[1], bytes):
        return output[1]
    return b""


def docker_sandbox_for(dot_id: str, settings: Settings, *, client: Any | None = None) -> DockerSandbox:
    """Host directories and the snapshot path for one dot. Does not start a container."""
    root = Path(settings.object_root) / dot_id / "sandbox"
    (root / "in").mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    return DockerSandbox(
        dot_id,
        SandboxMounts(root / "in", root / "skills"),
        image=settings.sandbox_image,
        client=client,
        object_root=Path(settings.object_root),
        idle_s=settings.sandbox_idle_s,
    )


class DockerSandbox(RunSandbox):
    def __init__(
        self,
        dot_id: str,
        mounts: SandboxMounts,
        *,
        image: str = "dot-sandbox",
        client: Any | None = None,
        object_root: Path | str = "var/objects",
        idle_s: int = 600,
    ) -> None:
        self._dot_id = dot_id
        self._mounts = mounts
        self._image = image
        self._client = client
        self._object_root = Path(object_root)
        self._idle_s = idle_s
        self._container: Any | None = None
        self._last_used = datetime.now(UTC)

    @property
    def id(self) -> str:
        if self._container is not None:
            return str(self._container.id)
        return f"dot-{self._dot_id}"

    def snapshot_path(self) -> Path:
        return self._object_root / self._dot_id / "sandbox" / "work.tar"

    def _docker(self) -> Any:
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def run_kwargs(self) -> dict[str, Any]:
        """Container settings. Kept separate so the isolation contract is testable."""
        return {
            "image": self._image,
            "command": ["sleep", "infinity"],
            "detach": True,
            "network_mode": "none",
            "read_only": True,
            "tmpfs": {WORK_DIR: "rw,size=256m,uid=1000,gid=1000", "/tmp": "rw,size=64m"},
            "volumes": {
                str(self._mounts.input_dir.resolve()): {"bind": IN_DIR, "mode": "ro"},
                str(self._mounts.skills_dir.resolve()): {"bind": SKILLS_DIR, "mode": "ro"},
            },
            "environment": {},
            "user": "runner",
            "working_dir": WORK_DIR,
            "mem_limit": "1g",
            "nano_cpus": 1_000_000_000,
            "pids_limit": 256,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "labels": {"dot.sandbox": self._dot_id},
            "name": _container_name(self._dot_id),
        }

    def start(self) -> None:
        if self._container is not None:
            return
        label = f"dot.sandbox={self._dot_id}"
        for old in self._docker().containers.list(all=True, filters={"label": label}):
            old.remove(force=True)
        self._container = self._docker().containers.run(**self.run_kwargs())
        try:
            self._restore_work()
        except Exception:
            self.close()
            raise
        self._touch()

    def close(self) -> None:
        if self._container is not None:
            self._container.remove(force=True)
            self._container = None

    def suspend_if_idle(self, now: datetime | None = None) -> bool:
        """Snapshot and stop once the sandbox has been unused for idle_s."""
        if self._container is None:
            return False
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if (current - self._last_used).total_seconds() < self._idle_s:
            return False
        self.suspend()
        return True

    def suspend(self) -> None:
        """Write /work to object storage, then remove the container."""
        if self._container is None:
            return
        data = self._snapshot_work()
        path = self.snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        self.close()

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        try:
            seconds = timeout or DEFAULT_TIMEOUT_S
            wrapped = f"{CLEAN_ENV} timeout {int(seconds)} sh -c {shlex.quote(command)}"
            result = self._run(wrapped)
            output = _stdout(result).decode("utf-8", errors="replace")
            if result.exit_code == 124:
                output += f"\n[timed out after {seconds}s]"
            text, truncated = truncate(output)
            return ExecuteResponse(output=text, exit_code=result.exit_code, truncated=truncated)
        finally:
            self._touch_if_running()

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        # The archive API refuses a read-only root even for the /work tmpfs,
        # so file bytes go in through exec, base64 in chunks.
        try:
            container = self._require()
            responses: list[FileUploadResponse] = []
            for path, content in files:
                normal = posixpath.normpath(path)
                if not (normal == WORK_DIR or normal.startswith(f"{WORK_DIR}/")):
                    responses.append(FileUploadResponse(path=path, error="permission_denied"))
                    continue
                ok = self._write_bytes(container, normal, content)
                responses.append(FileUploadResponse(path=path, error=None if ok else "permission_denied"))
            return responses
        finally:
            self._touch_if_running()

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        try:
            container = self._require()
            responses: list[FileDownloadResponse] = []
            for path in paths:
                quoted = shlex.quote(path)
                probe = f"if [ -d {quoted} ]; then exit 3; elif [ -f {quoted} ]; then base64 {quoted}; else exit 4; fi"
                result = container.exec_run(["sh", "-c", probe], workdir=WORK_DIR, demux=False)
                if result.exit_code == 3:
                    responses.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
                elif result.exit_code != 0:
                    responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                else:
                    content = base64.b64decode(b"".join(_stdout(result).split()))
                    responses.append(FileDownloadResponse(path=path, content=content, error=None))
            return responses
        finally:
            self._touch_if_running()

    def _touch(self) -> None:
        self._last_used = datetime.now(UTC)

    def _touch_if_running(self) -> None:
        if self._container is not None:
            self._touch()

    def _require(self) -> Any:
        if self._container is None:
            raise RuntimeError("sandbox is not started")
        return self._container

    def _run(self, script: str, *, demux: bool = False) -> Any:
        return self._require().exec_run(["sh", "-c", script], workdir=WORK_DIR, demux=demux)

    def _check(self, result: Any, action: str) -> None:
        if result.exit_code == 0:
            return
        err = _stderr(result).decode("utf-8", errors="replace").strip()
        message = f"{action} failed ({result.exit_code})"
        if err:
            message = f"{message}: {err[:500]}"
        raise RuntimeError(message)

    def _write_bytes(self, container: Any, path: str, content: bytes) -> bool:
        target = shlex.quote(path)
        encoded = base64.b64encode(content).decode()
        steps = [f"mkdir -p {shlex.quote(posixpath.dirname(path))} && : > {target}"]
        steps += [f"printf %s {shlex.quote(chunk)} | base64 -d >> {target}" for chunk in _chunks(encoded)]
        return all(container.exec_run(["sh", "-c", step], workdir=WORK_DIR).exit_code == 0 for step in steps)

    def _snapshot_work(self) -> bytes:
        created = self._run(f"{CLEAN_ENV} tar -C {WORK_DIR} -cf {_SNAPSHOT_TAR} .", demux=True)
        self._check(created, "snapshot /work")
        encoded = self._run(f"{CLEAN_ENV} base64 -w 0 {_SNAPSHOT_TAR}", demux=True)
        self._check(encoded, "read /work snapshot")
        self._run(f"{CLEAN_ENV} rm -f {_SNAPSHOT_TAR}")
        try:
            data = base64.b64decode(b"".join(_stdout(encoded).split()), validate=True)
        except binascii.Error as exc:
            raise RuntimeError("sandbox work snapshot is not valid base64") from exc
        if not data:
            raise RuntimeError("sandbox work snapshot is empty")
        return data

    def _restore_work(self) -> None:
        path = self.snapshot_path()
        if not path.is_file():
            return
        container = self._require()
        if not self._write_bytes(container, _SNAPSHOT_TAR, path.read_bytes()):
            raise RuntimeError("restore /work failed while writing the snapshot")
        extracted = self._run(
            f"{CLEAN_ENV} tar -C {WORK_DIR} --no-same-owner -xf {_SNAPSHOT_TAR}",
            demux=True,
        )
        self._check(extracted, "restore /work")
        self._run(f"{CLEAN_ENV} rm -f {_SNAPSHOT_TAR}")
