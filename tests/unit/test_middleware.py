from __future__ import annotations

from pathlib import Path

from langchain_core.messages import ToolMessage

from dot.middleware.offload import OffloadMiddleware
from dot.middleware.redaction import Redactor


def test_redactor_masks_configured_secrets_and_token_shapes() -> None:
    redactor = Redactor(["supersecret"])
    assert redactor.text("the value is supersecret") == "the value is [REDACTED]"
    assert redactor.text("sk-" + "a" * 20) == "[REDACTED]"
    assert redactor.text("api_key=abcd") == "api_key=[REDACTED]"
    assert redactor.text("short") == "short"


def test_offload_stores_long_tool_results(tmp_path: Path) -> None:
    middleware = OffloadMiddleware(tmp_path, max_chars=10)
    original = ("x" * 250) + "TAIL"
    result = middleware._offload(ToolMessage(content=original, tool_call_id="call-1", name="web_search"))
    assert isinstance(result, ToolMessage)
    assert "TAIL" not in str(result.content)
    assert "artifact tool-" in str(result.content)
    stored = list(tmp_path.glob("tool-*.txt"))
    assert len(stored) == 1
    assert stored[0].read_text(encoding="utf-8") == original
