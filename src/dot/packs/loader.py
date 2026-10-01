"""Load a pack directory, or refuse it with a message that says why.

An unknown tool, an untagged tool, or a profile that grants an effect the
policy blocks fails the load. ``seed_store`` copies ``skills/`` and ``wiki/``
into the LangGraph store for a new dot, in the shape ``StoreBackend`` reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from deepagents.backends.utils import create_file_data
from langgraph.store.base import BaseStore
from pydantic import ValidationError

from dot.safety.policy import resolve_decision
from dot.tools.effects import Effect
from dot.tools.registry import ToolRegistry, builtin_registry

from .schema import Decision, LoadedPack, McpConfig, Pack, Policy, Profile

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MCP = REPO_ROOT / "config" / "mcp.yaml"

# StoreBackend namespace components. Same pattern the backend itself allows.
_NAMESPACE = re.compile(r"^[A-Za-z0-9\-_.@+:~]+$")


class PackLoadError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def memories_namespace(dot_id: str) -> tuple[str, str]:
    """Namespace for the ``/memories/`` route. Keys keep the path under that prefix."""
    return (dot_id, "memories")


def wiki_namespace(dot_id: str) -> tuple[str, str]:
    """Namespace for the ``/wiki/`` route. Keys keep the path under that prefix."""
    return (dot_id, "wiki")


def load_pack(
    pack_dir: Path,
    registry: ToolRegistry | None = None,
    mcp_path: Path | None = None,
) -> LoadedPack:
    pack_dir = pack_dir.resolve()
    raw = _mapping(pack_dir / "pack.yaml")
    try:
        pack = Pack.model_validate(raw)
    except ValidationError as exc:
        raise PackLoadError([f"pack.yaml: {exc}"]) from exc

    errors: list[str] = []
    if pack_dir.name != pack.name:
        errors.append(f"directory {pack_dir.name!r} does not match pack name {pack.name!r}")

    policy_path = pack_dir / pack.policy
    try:
        policy = Policy.model_validate(_mapping(policy_path))
    except PackLoadError as exc:
        errors.extend(exc.errors)
        policy = None
    except ValidationError as exc:
        errors.append(f"{pack.policy}: {exc}")
        policy = None

    persona_path = pack_dir / pack.persona
    persona_text = ""
    if not persona_path.is_file():
        errors.append(f"missing persona file {pack.persona}")
    else:
        persona_text = persona_path.read_text(encoding="utf-8")

    skill_files = _collect_skills(pack_dir, pack.skills, errors)
    wiki_files = _collect_wiki(pack_dir, pack.wiki, errors)

    reg = (registry if registry is not None else builtin_registry()).copy()
    mcp = _load_mcp(mcp_path or DEFAULT_MCP, errors)
    mcp_tools: set[str] = set()
    if mcp is not None:
        mcp_tools = _merge_mcp(pack, mcp, reg, errors)
    _check_tool_refs(pack, reg, errors)
    _check_profiles(pack, policy, reg, mcp_tools, errors)

    if errors or policy is None:
        raise PackLoadError(errors)
    return LoadedPack(
        root=pack_dir,
        pack=pack,
        policy=policy,
        persona_text=persona_text,
        skill_files=tuple(skill_files),
        wiki_files=tuple(wiki_files),
    )


def seed_store(loaded: LoadedPack, store: BaseStore, dot_id: str) -> None:
    """Copy the pack's skills and wiki into the store for one new dot."""
    if not _NAMESPACE.fullmatch(dot_id):
        raise PackLoadError([f"dot id {dot_id!r} cannot be used as a store namespace"])
    skills_root = loaded.root / loaded.pack.skills
    for path in loaded.skill_files:
        relative = path.relative_to(skills_root).as_posix()
        store.put(memories_namespace(dot_id), f"/skills/{relative}", _file_value(path), index=False)
    wiki_root = loaded.root / loaded.pack.wiki
    for path in loaded.wiki_files:
        relative = path.relative_to(wiki_root).as_posix()
        store.put(wiki_namespace(dot_id), f"/{relative}", _file_value(path), index=False)


def _file_value(path: Path) -> dict[str, Any]:
    return dict(create_file_data(path.read_text(encoding="utf-8")))


def _mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PackLoadError([f"missing file {path.name}"])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PackLoadError([f"{path.name} must be a mapping"])
    return data


def _load_mcp(path: Path, errors: list[str]) -> McpConfig | None:
    try:
        raw = _mapping(path)
    except PackLoadError as exc:
        errors.extend(exc.errors)
        return None
    try:
        return McpConfig.model_validate(raw)
    except ValidationError as exc:
        errors.append(f"{path.name}: {exc}")
        return None


def _merge_mcp(pack: Pack, mcp: McpConfig, registry: ToolRegistry, errors: list[str]) -> set[str]:
    names: set[str] = set()
    for server_name in pack.tools.mcp:
        server = mcp.servers.get(server_name)
        if server is None:
            errors.append(f"unknown MCP server {server_name!r}")
            continue
        for tool_name, effect in server.tools.items():
            if tool_name in registry:
                errors.append(f"MCP tool {tool_name!r} on server {server_name!r} collides with an existing tool")
                continue
            if effect is None:
                errors.append(f"untagged tool {tool_name!r} (mcp server {server_name})")
            registry.register(tool_name, effect)
            names.add(tool_name)
    return names


def _check_tool_refs(pack: Pack, registry: ToolRegistry, errors: list[str]) -> None:
    refs: list[tuple[str, str]] = [(name, "tools.native") for name in pack.tools.native]
    seen_subagents: set[str] = set()
    for subagent in pack.subagents:
        if subagent.name in seen_subagents:
            errors.append(f"duplicate subagent {subagent.name!r}")
        seen_subagents.add(subagent.name)
        refs.extend((name, f"subagent {subagent.name}") for name in subagent.tools)
    for profile_name, profile in pack.profiles.items():
        if isinstance(profile.tools, list):
            refs.extend((name, f"profile {profile_name}") for name in profile.tools)
        if isinstance(profile.subagents, list):
            known = {subagent.name for subagent in pack.subagents}
            for name in profile.subagents:
                if name not in known:
                    errors.append(f"profile {profile_name!r} names unknown subagent {name!r}")
    for schedule in pack.schedules:
        if schedule.profile not in pack.profiles:
            errors.append(f"schedule {schedule.name!r} names unknown profile {schedule.profile!r}")

    seen: set[tuple[str, str]] = set()
    for name, where in refs:
        if (name, where) in seen:
            continue
        seen.add((name, where))
        if name not in registry:
            errors.append(f"unknown tool {name!r} ({where})")
        elif registry.effect(name) is None:
            errors.append(f"untagged tool {name!r} ({where})")


def _check_profiles(
    pack: Pack,
    policy: Policy | None,
    registry: ToolRegistry,
    mcp_tools: set[str],
    errors: list[str],
) -> None:
    if policy is None:
        return
    for profile_name, profile in pack.profiles.items():
        for effect in profile.effects or []:
            if policy.defaults.for_effect(effect) is Decision.block:
                errors.append(f"profile {profile_name!r} grants blocked effect {effect.value!r}")
        for tool_name in _granted_tools(pack, profile, registry, mcp_tools):
            tool_effect = registry.effect(tool_name)
            if tool_effect is None:
                continue
            if _decision(policy, tool_name, tool_effect) is Decision.block:
                errors.append(f"profile {profile_name!r} grants {tool_name!r}, which policy blocks")


def _granted_tools(pack: Pack, profile: Profile, registry: ToolRegistry, mcp_tools: set[str]) -> list[str]:
    names: set[str] = set()
    if profile.tools == "*":
        names.update(pack.tools.native)
        names.update(mcp_tools)
    elif isinstance(profile.tools, list):
        names.update(profile.tools)
    subagents = pack.subagents
    if isinstance(profile.subagents, list):
        wanted = set(profile.subagents)
        subagents = [subagent for subagent in pack.subagents if subagent.name in wanted]
    elif profile.subagents is None:
        subagents = []
    for subagent in subagents:
        names.update(subagent.tools)
    tagged = [name for name in names if name in registry and registry.effect(name) is not None]
    if profile.effects:
        allowed = set(profile.effects)
        tagged = [name for name in tagged if registry.effect(name) in allowed]
    return sorted(tagged)


def _decision(policy: Policy, tool_name: str, effect: Effect) -> Decision:
    return resolve_decision(policy, tool_name, effect)


def _collect_skills(pack_dir: Path, relative: str, errors: list[str]) -> list[Path]:
    root = pack_dir / relative
    if not root.is_dir():
        errors.append(f"missing skills directory {relative}")
        return []
    files: list[Path] = []
    skill_dirs = sorted(path for path in root.iterdir() if path.is_dir())
    if not skill_dirs:
        errors.append(f"{relative} has no skills")
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            errors.append(f"skill {skill_dir.name!r} has no SKILL.md")
            continue
        text = skill_md.read_text(encoding="utf-8")
        name = _frontmatter_name(text)
        if name != skill_dir.name:
            errors.append(f"skill {skill_dir.name!r} frontmatter name is {name!r}")
        files.append(skill_md)
        files.extend(sorted(path for path in skill_dir.rglob("*") if path.is_file() and path != skill_md))
    return files


def _collect_wiki(pack_dir: Path, relative: str, errors: list[str]) -> list[Path]:
    root = pack_dir / relative
    if not root.is_dir():
        errors.append(f"missing wiki directory {relative}")
        return []
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        errors.append(f"{relative} is empty")
    return files


def _frontmatter_name(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    data = yaml.safe_load(text[3:end])
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    return name if isinstance(name, str) else None
