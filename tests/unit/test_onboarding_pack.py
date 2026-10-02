from __future__ import annotations

from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import Decision, Effect
from dot.safety.policy import resolve_decision
from dot.tools.registry import builtin_registry

PACK = REPO_ROOT / "packs" / "onboarding-ops"


def test_onboarding_ops_pack_loads() -> None:
    loaded = load_pack(PACK)
    assert loaded.pack.name == "onboarding-ops"
    skills = sorted(path.parent.name for path in loaded.skill_files if path.name == "SKILL.md")
    assert skills == ["intake", "sponsor-questions"]
    assert sorted(path.name for path in loaded.wiki_files) == ["affiliate-onboarding.md", "sponsors.md"]
    persona = loaded.persona_text.lower()
    assert "gate" in persona
    assert "data" in persona


def test_sweeps_reach_only_read_tools() -> None:
    pack = load_pack(PACK).pack
    registry = builtin_registry()
    sweeps = [s for s in pack.schedules if s.kind == "sweep"]
    assert {s.name for s in sweeps} == {"intake-sweep", "status-sweep"}
    for schedule in sweeps:
        assert schedule.profile is not None
        profile = pack.profiles[schedule.profile]
        assert profile.effects == [Effect.read]
        assert profile.tools is None
        assert registry.effect("list_drops") is Effect.read


def test_daily_fires_before_intake_can_pause_the_dot() -> None:
    # A pending start card pauses every schedule of the dot, so the daily digest runs first.
    crons = {s.name: s.cron for s in load_pack(PACK).pack.schedules}
    assert crons["daily"] == "45 6 * * 1-5"
    assert crons["intake-sweep"] == "*/15 7-17 * * 1-5"
    assert crons["intake"] == "5-59/15 7-17 * * 1-5"


def test_digests_read_their_sweeps_findings() -> None:
    pack = load_pack(PACK).pack
    assert pack.schedule("intake").findings_from == ["intake-sweep"]
    assert pack.schedule("daily").findings_from == ["status-sweep"]


def test_policy_gates_writes_and_sends() -> None:
    loaded = load_pack(PACK)
    registry = builtin_registry()
    for name, expected in (
        ("start_run", Decision.approve),
        ("send_email", Decision.approve),
        ("draft_email", Decision.allow),
        ("list_runs", Decision.allow),
    ):
        assert resolve_decision(loaded.policy, name, registry.effect(name)) is expected


def test_no_tool_can_answer_a_gate() -> None:
    pack = load_pack(PACK).pack
    names = set(pack.tools.native)
    for profile in pack.profiles.values():
        if isinstance(profile.tools, list):
            names.update(profile.tools)
    assert not [name for name in names if "gate" in name]
