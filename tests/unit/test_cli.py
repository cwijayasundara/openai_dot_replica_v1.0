"""``dot create`` and ``dot run`` against the scripted model. No database and no live model."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from dot.config import Settings
from dot.surfaces.cli import execute
from tests.support.scripted_model import ScriptedChatModel, say


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, object_root=str(tmp_path / "objects"), database_url=None)  # type: ignore[call-arg]


def test_create_prints_a_dot_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = execute(["create", "research-analyst", "--owner", "ada"], settings=_settings(tmp_path))
    assert code == 0
    assert capsys.readouterr().out.strip().startswith("dot-")


def test_run_prints_the_scripted_answer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[str] = []

    def step(messages: list[BaseMessage]) -> AIMessage:
        seen.extend(str(message.content) for message in messages)
        return say("Hello from the dot.")

    model = ScriptedChatModel(script=[step, step, step, step])
    code = execute(["run", "research-analyst", "hello"], settings=_settings(tmp_path), model=model)
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "Hello from the dot."
    assert any("[web] hello" in text for text in seen)


def test_unknown_pack_and_tail_without_a_database_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    assert execute(["create", "no-such-pack"], settings=settings) == 2
    assert execute(["tail", "dot-1"], settings=settings) == 2
    assert "DOT_DATABASE_URL is required" in capsys.readouterr().err
