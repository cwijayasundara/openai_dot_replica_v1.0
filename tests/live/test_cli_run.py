"""``dot run research-analyst "hello"`` against the configured live model."""

from __future__ import annotations

from pathlib import Path

import pytest

from dot.config import get_settings
from dot.surfaces.cli import execute

pytestmark = pytest.mark.live


def test_dot_run_research_analyst_hello(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = get_settings().model_copy(update={"database_url": None, "object_root": str(tmp_path / "objects")})
    code = execute(["run", "research-analyst", "hello"], settings=settings)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out.strip()
