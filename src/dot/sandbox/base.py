"""One isolated computer per dot.

Layout inside every backend:
    /in      inputs, read-only
    /work    scratch space, the only writable path
    /skills  skill files, read-only

Building an agent must not start a container. ``LazySandbox`` starts one on first use.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox

IN_DIR, WORK_DIR, SKILLS_DIR = "/in", "/work", "/skills"
MAX_OUTPUT_CHARS = 100_000
DEFAULT_TIMEOUT_S = 120


@dataclass(frozen=True, slots=True)
class SandboxMounts:
    input_dir: Path
    skills_dir: Path


def truncate(output: str) -> tuple[str, bool]:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output, False
    return output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]", True


class RunSandbox(BaseSandbox):
    """A sandbox that owns a container for the life of one dot (or one job)."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class LazySandbox(BaseSandbox):
    """Delegates to a dot's sandbox, starting it on first use."""

    def __init__(self, get: Callable[[], RunSandbox], dot_id: str) -> None:
        self._get = get
        self._dot_id = dot_id

    @property
    def id(self) -> str:
        return f"lazy-{self._dot_id}"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._get().execute(command, timeout=timeout)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._get().upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._get().download_files(paths)

    # File operations go to the real sandbox so a backend that specialises
    # them is honoured; the inherited versions would bypass it.
    def ls(self, *args: Any, **kwargs: Any) -> Any:
        return self._get().ls(*args, **kwargs)

    def read(self, *args: Any, **kwargs: Any) -> Any:
        return self._get().read(*args, **kwargs)

    def write(self, *args: Any, **kwargs: Any) -> Any:
        return self._get().write(*args, **kwargs)

    def edit(self, *args: Any, **kwargs: Any) -> Any:
        return self._get().edit(*args, **kwargs)

    def grep(self, *args: Any, **kwargs: Any) -> Any:
        return self._get().grep(*args, **kwargs)

    def glob(self, *args: Any, **kwargs: Any) -> Any:
        return self._get().glob(*args, **kwargs)


def unconfigured_sandbox(dot_id: str) -> RunSandbox:
    """Factory used until a Docker or OpenShell backend exists. It never starts one."""
    raise RuntimeError(f"sandbox for {dot_id} is not configured")
