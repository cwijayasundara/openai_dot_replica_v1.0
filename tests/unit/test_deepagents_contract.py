"""Reflection checks SKILL.md edits with deepagents' private parser. A deepagents upgrade must keep its behaviour."""

from __future__ import annotations

from deepagents.middleware.skills import _parse_skill_metadata

from dot.packs.loader import REPO_ROOT

PATH = "/memories/skills/email-drafting/SKILL.md"


def test_a_valid_skill_parses_and_broken_frontmatter_does_not() -> None:
    valid = (REPO_ROOT / "packs/research-analyst/skills/email-drafting/SKILL.md").read_text(encoding="utf-8")
    assert _parse_skill_metadata(valid, PATH, "email-drafting") is not None
    assert _parse_skill_metadata("---\nname: [unclosed\n---\nbody\n", PATH, "email-drafting") is None
    assert _parse_skill_metadata("no frontmatter at all\n", PATH, "email-drafting") is None
