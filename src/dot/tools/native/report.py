"""Save a brief. The model gets the artifact id back, not the body."""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from dot.tools.native.deps import ToolDeps
from dot.tools.results import fail, ok

MAX_CHARS = 100_000


def build_write_report(deps: ToolDeps) -> BaseTool:
    def write_report(title: str, markdown: str) -> str:
        """Save a markdown brief and return its artifact id. This does not send the brief."""
        if not title.strip() or not markdown.strip():
            return fail("title and markdown are required")
        if len(markdown) > MAX_CHARS:
            return fail("report is too long")
        body = f"# {title.strip()}\n\n{markdown}"
        artifact_id = deps.artifacts.put_text(body)
        return ok(artifact_id=artifact_id, title=title.strip(), byte_count=len(body.encode("utf-8")))

    return StructuredTool.from_function(write_report, name="write_report")
