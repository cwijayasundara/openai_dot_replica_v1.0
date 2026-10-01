"""Native tools for v1, each registered with one effect."""

from __future__ import annotations

from langchain_core.tools import BaseTool

from dot.tools.native.deps import ToolDeps
from dot.tools.native.email import build_draft_email, build_send_email
from dot.tools.native.fetch import build_fetch_url
from dot.tools.native.report import build_write_report
from dot.tools.native.search import build_web_search
from dot.tools.native.slack import build_slack_post
from dot.tools.registry import ToolRegistry, builtin_registry

NATIVE_TOOL_NAMES = (
    "web_search",
    "fetch_url",
    "write_report",
    "draft_email",
    "send_email",
    "slack_post",
)


def build_native_tools(deps: ToolDeps) -> list[BaseTool]:
    return [
        build_web_search(deps),
        build_fetch_url(deps),
        build_write_report(deps),
        build_draft_email(deps),
        build_send_email(deps),
        build_slack_post(deps),
    ]


def native_registry(deps: ToolDeps) -> ToolRegistry:
    """Builtin effects plus a callable for each v1 native tool.

    ``execute`` keeps its effect and has no callable until the sandbox backend exists.
    """
    registry = builtin_registry()
    for tool in build_native_tools(deps):
        registry.register(tool.name, registry.effect(tool.name), tool)
    return registry
