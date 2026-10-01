"""Docker daemon: isolation settings hold, and suspend/restore keeps /work."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from dot.sandbox.base import SandboxMounts
from dot.sandbox.docker_backend import DockerSandbox

pytestmark = pytest.mark.docker


def test_suspend_and_restore_keeps_work(tmp_path: Path) -> None:
    dot_id = f"dot-x1{uuid4().hex[:8]}"
    for name in ("in", "skills"):
        (tmp_path / name).mkdir()
    sandbox = DockerSandbox(
        dot_id,
        SandboxMounts(tmp_path / "in", tmp_path / "skills"),
        object_root=tmp_path / "objects",
        idle_s=0,
    )
    try:
        sandbox.start()
        sandbox._container.reload()
        host = sandbox._container.attrs["HostConfig"]
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert "ALL" in host["CapDrop"]
        assert "/work" in host["Tmpfs"]
        assert sandbox._container.labels["dot.sandbox"] == dot_id

        uploaded = sandbox.upload_files([("/work/nested/note.txt", b"kept-intact\n")])
        assert uploaded[0].error is None
        wrote = sandbox.execute("printf extra > extra.txt")
        assert wrote.exit_code == 0, wrote.output

        assert sandbox.suspend_if_idle() is True
        assert sandbox.snapshot_path().is_file()
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.execute("cat extra.txt")

        sandbox.start()
        note, extra = sandbox.download_files(["/work/nested/note.txt", "/work/extra.txt"])
        assert note.error is None and note.content == b"kept-intact\n"
        assert extra.error is None and extra.content == b"extra"
    finally:
        sandbox.close()
