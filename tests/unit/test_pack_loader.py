from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from deepagents.backends.store import StoreBackend
from langgraph.store.memory import InMemoryStore

from dot.packs.loader import REPO_ROOT, PackLoadError, load_pack, memories_namespace, seed_store, wiki_namespace
from dot.packs.schema import Decision
from dot.tools.registry import builtin_registry

PACK = REPO_ROOT / "packs" / "research-analyst"


def test_research_analyst_pack_loads() -> None:
    loaded = load_pack(PACK)
    assert loaded.pack.name == "research-analyst"
    assert loaded.policy.defaults.credential is Decision.block
    assert loaded.policy.defaults.external is Decision.approve
    skill_names = sorted(path.parent.name for path in loaded.skill_files if path.name == "SKILL.md")
    assert skill_names == ["brief-writing", "email-drafting", "research-method"]
    assert loaded.wiki_files == (PACK / "wiki" / "organisation.md",)
    assert "research analyst" in loaded.persona_text.lower()


def test_seed_store_is_readable_by_the_store_backend() -> None:
    loaded = load_pack(PACK)
    store = InMemoryStore()
    seed_store(loaded, store, "dot-1")
    memories = StoreBackend(store=store, namespace=lambda _rt: memories_namespace("dot-1"))
    wiki = StoreBackend(store=store, namespace=lambda _rt: wiki_namespace("dot-1"))
    skill = memories.read("/skills/research-method/SKILL.md")
    page = wiki.read("/organisation.md")
    assert skill.error is None
    assert skill.file_data is not None
    assert "Research method" in skill.file_data["content"]
    assert page.error is None
    assert page.file_data is not None
    assert "Organisation" in page.file_data["content"]


def test_unknown_tool_fails(tmp_path: Path) -> None:
    root = _write_pack(tmp_path / "research-analyst", native=["web_search", "nope"])
    with pytest.raises(PackLoadError, match=r"unknown tool 'nope'"):
        load_pack(root)


def test_untagged_tool_fails(tmp_path: Path) -> None:
    registry = builtin_registry()
    registry.register("mystery", None)
    root = _write_pack(tmp_path / "research-analyst", native=["mystery"])
    with pytest.raises(PackLoadError, match=r"untagged tool 'mystery'"):
        load_pack(root, registry=registry)


def test_profile_granting_a_blocked_effect_fails(tmp_path: Path) -> None:
    root = _write_pack(
        tmp_path / "research-analyst",
        native=["web_search"],
        profiles={"sweep": {"effects": ["credential"], "subagents": []}},
    )
    with pytest.raises(PackLoadError, match=r"profile 'sweep' grants blocked effect 'credential'"):
        load_pack(root)


def _sweep(**values: object) -> dict[str, object]:
    return {"name": "sweep", "cron": "*/30 7-22 * * 1-5", "profile": "sweep", "prompt": "Sweep.", **values}


def test_a_bad_cron_fails(tmp_path: Path) -> None:
    root = _write_pack(
        tmp_path / "research-analyst",
        native=["web_search"],
        profiles={"sweep": {"effects": ["read"]}},
        schedules=[_sweep(cron="0 9 * * */2")],
    )
    with pytest.raises(PackLoadError, match=r"schedule 'sweep'.*steps are not supported"):
        load_pack(root)


def test_a_sweep_that_could_send_fails(tmp_path: Path) -> None:
    root = _write_pack(
        tmp_path / "research-analyst",
        native=["web_search", "slack_post"],
        profiles={"sweep": {"tools": ["web_search", "slack_post"]}},
        schedules=[_sweep()],
    )
    with pytest.raises(PackLoadError, match=r"sweep 'sweep' uses profile 'sweep', which grants 'slack_post'"):
        load_pack(root)


def test_a_sweep_whose_subagent_could_send_fails(tmp_path: Path) -> None:
    # The profile's effect filter does not apply inside a subagent, so the subagent's own tools count.
    root = _write_pack(
        tmp_path / "research-analyst",
        native=["web_search", "send_email"],
        subagents=[{"name": "mailer", "model": "fast", "tools": ["send_email"], "description": "Mails."}],
        profiles={"sweep": {"effects": ["read"], "subagents": ["mailer"]}},
        schedules=[_sweep()],
    )
    with pytest.raises(PackLoadError, match=r"which grants 'send_email'"):
        load_pack(root)


def test_a_digest_may_draft(tmp_path: Path) -> None:
    root = _write_pack(
        tmp_path / "research-analyst",
        native=["web_search", "draft_email"],
        profiles={"digest": {"effects": ["read", "draft"]}},
        schedules=[{"name": "digest", "kind": "digest", "cron": "45 8 * * 1-5", "profile": "digest", "prompt": "D."}],
    )
    assert load_pack(root).pack.schedule("digest").kind == "digest"


def _write_pack(
    root: Path,
    *,
    native: list[str],
    profiles: dict[str, object] | None = None,
    subagents: list[dict[str, object]] | None = None,
    schedules: list[dict[str, object]] | None = None,
) -> Path:
    root.mkdir(parents=True)
    (root / "persona.md").write_text("# Analyst\n\nCalm.\n", encoding="utf-8")
    skill = root / "skills" / "research-method"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: research-method\ndescription: Research a question.\n---\n\n# Research method\n",
        encoding="utf-8",
    )
    wiki = root / "wiki"
    wiki.mkdir()
    (wiki / "organisation.md").write_text("# Organisation\n", encoding="utf-8")
    (root / "policy.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "read": "allow",
                    "draft": "allow",
                    "write": "approve",
                    "external": "approve",
                    "financial": "approve",
                    "credential": "block",
                },
                "tools": {},
                "approvers": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": root.name,
                "persona": "persona.md",
                "models": {"supervisor": "glm-5p3", "heavy": "kimi-k3", "fast": "glm-5p3-flash"},
                "skills": "skills/",
                "wiki": "wiki/",
                "tools": {"native": native, "mcp": ["github"]},
                "subagents": subagents or [],
                "profiles": profiles or {"chat": {"tools": "*", "subagents": []}},
                "policy": "policy.yaml",
                "schedules": schedules or [],
            }
        ),
        encoding="utf-8",
    )
    return root
