"""OpenShell adapter, ported from langchain-ai/openshell-deepagent/src/backend.py.

Read-only inputs are baked into an image before creation: OpenShell filesystem
policy is static, so granting temporary write access and later sealing is unsafe.
The image builder must use the gateway's Docker daemon (or return a published
image reference for a remote gateway). No registry push is performed here.
"""

from __future__ import annotations

import base64
import hashlib
import io
import posixpath
import shlex
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from google.protobuf.json_format import ParseDict
from openshell import SandboxClient, SandboxSession
from openshell._proto import openshell_pb2, sandbox_pb2

from dot.config import Settings
from dot.persistence.db import AuditEvent

from .base import DEFAULT_TIMEOUT_S, WORK_DIR, RunSandbox, SandboxMounts, truncate

POLICY_ROOT = Path(__file__).resolve().parents[3] / "sandbox" / "policies"
if not POLICY_ROOT.is_dir():
    POLICY_ROOT = Path(__file__).parent / "policies"
# Preserve only OpenShell's proxy plumbing, never application credentials.
CLEAN_ENV = (
    "env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/work LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 "
    'HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" ALL_PROXY="$ALL_PROXY" '
    'http_proxy="$http_proxy" https_proxy="$https_proxy" all_proxy="$all_proxy" '
    'NO_PROXY="$NO_PROXY" no_proxy="$no_proxy" SSL_CERT_FILE="$SSL_CERT_FILE" '
    'SSL_CERT_DIR="$SSL_CERT_DIR" CURL_CA_BUNDLE="$CURL_CA_BUNDLE"'
)
ImageBuilder = Callable[[str, SandboxMounts], str]
AuditWriter = Callable[[AuditEvent], object]


def policy_for_profile(profile: str) -> str:
    # Capability profile names are not paths. Only explicit research gets egress.
    return "research" if profile == "research" else "default"


# The gateway rejects sandbox names longer than 19 characters ("dot-" + 15 hex).
_NAME_HEX = 15


def load_policy(name: str) -> sandbox_pb2.SandboxPolicy:
    if name not in {"default", "research"}:
        raise ValueError(f"unknown OpenShell policy {name!r}")
    data = yaml.safe_load((POLICY_ROOT / f"{name}.yaml").read_text())
    data["filesystem"] = data.pop("filesystem_policy")
    # CLI YAML uses friendly enum names; protobuf JSON requires wire names.
    for rule in data.get("network_policies", {}).values():
        for endpoint in rule.get("endpoints", []):
            for field, prefix in (
                ("tls", "NETWORK_TLS_MODE_"),
                ("enforcement", "NETWORK_ENFORCEMENT_MODE_"),
                ("access", "NETWORK_ACCESS_PRESET_"),
            ):
                if field in endpoint:
                    endpoint[field] = prefix + endpoint[field].upper().replace("-", "_")
    policy = sandbox_pb2.SandboxPolicy()
    ParseDict(data, policy)
    return policy


def build_input_image(base_image: str, mounts: SandboxMounts) -> str:
    """Copy and seal host files in a deterministic, local OCI image layer."""
    import docker

    if not base_image or any(c.isspace() for c in base_image):
        raise ValueError("invalid sandbox base image")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:

        def add(name: str, content: bytes, mode: int = 0o444) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), mode
            archive.addfile(info, io.BytesIO(content))

        add(
            "Dockerfile",
            (
                f"FROM {base_image}\nUSER root\nCOPY in/ /in/\nCOPY skills/ /skills/\n"
                "RUN chown -R root:root /in /skills && chmod -R a-w /in /skills\n"
                "USER runner\nWORKDIR /work\n"
            ).encode(),
        )
        for name, root in (("in", mounts.input_dir), ("skills", mounts.skills_dir)):
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o555
            archive.addfile(info)
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"sandbox input symlink is not allowed: {path}")
                if path.is_file():
                    add(f"{name}/{path.relative_to(root).as_posix()}", path.read_bytes())
                elif not path.is_dir():
                    raise ValueError(f"sandbox input is not a regular file: {path}")
    tag = f"dot-openshell-inputs:{hashlib.sha256(buffer.getvalue()).hexdigest()[:32]}"
    buffer.seek(0)
    client = docker.from_env()
    try:
        client.images.build(fileobj=buffer, custom_context=True, tag=tag, rm=True, network_mode="none")
    finally:
        client.close()
    return tag


def openshell_sandbox_for(
    dot_id: str,
    settings: Settings,
    *,
    profile: str = "chat",
    audit: AuditWriter,
    client: Any | None = None,
    image_builder: ImageBuilder = build_input_image,
) -> OpenShellSandbox:
    root = Path(settings.object_root) / dot_id / "sandbox"
    for name in ("in", "skills"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return OpenShellSandbox(
        dot_id,
        SandboxMounts(root / "in", root / "skills"),
        gateway=settings.openshell_gateway or "",
        image=settings.sandbox_image,
        policy=policy_for_profile(profile),
        audit=audit,
        client=client,
        image_builder=image_builder,
        object_root=Path(settings.object_root),
        idle_s=settings.sandbox_idle_s,
    )


class OpenShellSandbox(RunSandbox):
    def __init__(
        self,
        dot_id: str,
        mounts: SandboxMounts,
        *,
        gateway: str,
        audit: AuditWriter,
        image: str = "dot-sandbox",
        policy: str = "default",
        client: Any | None = None,
        image_builder: ImageBuilder = build_input_image,
        object_root: Path = Path("var/objects"),
        idle_s: int = 600,
    ) -> None:
        self._dot_id, self._mounts = dot_id, mounts
        self._gateway, self._image, self._policy = gateway, image, policy
        self._audit, self._client, self._image_builder = audit, client, image_builder
        self._owned_client = client is None
        self._session: Any | None = None
        self._name: str | None = None
        self._object_root, self._idle_s = object_root, idle_s
        self._last_used = datetime.now(UTC)

    @property
    def id(self) -> str:
        return str(self._session.id) if self._session is not None else f"dot-{self._dot_id}"

    def start(self) -> None:
        if self._session is not None:
            return
        policy = load_policy(self._policy)
        image = self._image_builder(self._image, self._mounts)
        if self._client is None:
            self._client = SandboxClient.from_active_cluster(cluster=self._gateway)
        try:
            spec = openshell_pb2.SandboxSpec(
                template=openshell_pb2.SandboxTemplate(image=image),
                policy=policy,
                command=["sleep", "infinity"],
            )
            ref = self._client.create(
                workspace="default",
                spec=spec,
                name=f"dot-{uuid4().hex[:_NAME_HEX]}",
                labels={"dot.sandbox": self._dot_id, "dot.policy": self._policy},
            )
            self._name = ref.name
            ready = self._client.wait_ready(ref.name, workspace="default")
            self._session = SandboxSession(self._client, ready)
            if self.snapshot_path().exists():
                uploaded = self.upload_files([("/work/.dot-restore.tar", self.snapshot_path().read_bytes())])
                if (
                    uploaded[0].error
                    or self.execute("tar -xf /work/.dot-restore.tar -C /work && rm /work/.dot-restore.tar").exit_code
                ):
                    raise RuntimeError("OpenShell work restore failed")
            self._last_used = datetime.now(UTC)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._name is not None:
            assert self._client is not None
            self._client.delete(self._name, workspace="default", allow_missing=True)
            self._name = None
        self._session = None
        if self._owned_client and self._client is not None:
            self._client.close()
            self._client = None

    def snapshot_path(self) -> Path:
        return self._object_root / self._dot_id / "sandbox" / "work.tar"

    def suspend_if_idle(self, now: datetime | None = None) -> bool:
        if self._session is None or ((now or datetime.now(UTC)) - self._last_used).total_seconds() < self._idle_s:
            return False
        self.suspend()
        return True

    def suspend(self) -> None:
        if self._session is None:
            return
        result = self._exec("tar -C /work --exclude=./.dot-snapshot.tar -cf /work/.dot-snapshot.tar .")
        if result.exit_code:
            raise RuntimeError(f"OpenShell snapshot failed: {result.stderr}")
        downloaded = self.download_files(["/work/.dot-snapshot.tar"])[0]
        if downloaded.error or downloaded.content is None:
            raise RuntimeError("OpenShell snapshot download failed")
        data = downloaded.content
        self._exec("rm /work/.dot-snapshot.tar")
        path = self.snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        self.close()

    def _record_error(self, tool: str, message: str) -> None:
        denied = any(
            word in message.lower()
            for word in (
                "denied",
                "blocked",
                "policy",
                "forbidden",
                "403 from proxy",
            )
        )
        self._audit(
            AuditEvent(
                id=0,
                dot_id=self._dot_id,
                at=datetime.now(UTC),
                actor="openshell",
                kind="sandbox_denial" if denied else "sandbox_error",
                tool=tool,
                decision="block" if denied else "error",
                detail={"sandbox_id": self.id, "policy": self._policy, "error": message[:2000]},
            )
        )

    def _exec(self, command: str, *, timeout: int | None = None, stdin: bytes | None = None) -> Any:
        if self._session is None:
            raise RuntimeError("sandbox is not started")
        try:
            result = self._session.exec(
                ["sh", "-c", f"{CLEAN_ENV} sh -c {shlex.quote(command)}"],
                workdir=WORK_DIR,
                stdin=stdin,
                no_login_shell=True,
                timeout_seconds=timeout if timeout is not None else DEFAULT_TIMEOUT_S,
            )
        except Exception as exc:
            self._record_error("execute", str(exc))
            raise
        finally:
            self._last_used = datetime.now(UTC)
        if result.exit_code or "policy_denied" in result.stdout:
            self._record_error("execute", result.stderr or result.stdout or f"exit code {result.exit_code}")
        return result

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        result = self._exec(command, timeout=timeout)
        output = result.stdout
        if result.stderr:
            output = f"{output}\n{result.stderr}" if output else result.stderr
        output, truncated = truncate(output)
        return ExecuteResponse(output=output, exit_code=result.exit_code, truncated=truncated)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses = []
        for path, content in files:
            normal = posixpath.normpath(path)
            if not normal.startswith("/work/"):
                self._record_error("upload_files", f"write denied outside /work: {path}")
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
                continue
            result = self._exec(
                f"mkdir -p {shlex.quote(posixpath.dirname(normal))} && cat > {shlex.quote(normal)}",
                stdin=content,
            )
            responses.append(
                FileUploadResponse(
                    path=path,
                    error=None if result.exit_code == 0 else "permission_denied",
                )
            )
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = []
        for path in paths:
            quoted = shlex.quote(path)
            result = self._exec(
                f"if [ -d {quoted} ]; then exit 3; elif [ -f {quoted} ]; then base64 {quoted}; else exit 4; fi"
            )
            if result.exit_code:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=None,
                        error="is_directory" if result.exit_code == 3 else "file_not_found",
                    )
                )
            else:
                responses.append(FileDownloadResponse(path=path, content=base64.b64decode(result.stdout), error=None))
        return responses
