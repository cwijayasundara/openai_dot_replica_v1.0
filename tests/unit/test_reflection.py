"""L2: reflection drafts memory edits from episodes; code checks each one and writes nothing to memory."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends.utils import create_file_data
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from dot.assembly import GraphRuntime, build_dot_agent, build_graph_runtime
from dot.config import Settings
from dot.memory.reflection import AGENTS_PATH, MemoryFiles, run_reflection
from dot.middleware.redaction import Redactor
from dot.packs.loader import REPO_ROOT, load_pack
from dot.packs.schema import REFLECTION_PROFILE, Schedule
from dot.persistence.db import Dot, Episode, MemoryRepositories, MemoryVersion
from dot.proactive.scheduler import trigger
from dot.runtime.turns import InMemoryEventChannel
from dot.runtime.worker import run_agent_turn
from dot.surfaces.dots import create_dot
from tests.support.scripted_model import ScriptedChatModel

NOW = datetime(2026, 10, 2, 2, 0, tzinfo=UTC)
LONG = " ".join(["word"] * 200)
SHORT = " ".join(["word"] * 60)
SCHEDULE = Schedule(name="reflection", kind="reflection", cron="0 2 * * *", prompt="Learn email style.")
SKILL = "/memories/skills/email-drafting/SKILL.md"


class Rig:
    def __init__(self, tmp_path: Path, structured: list[Any], *, secrets: tuple[str, ...] = ()) -> None:
        self.settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=None,
            object_root=str(tmp_path),
            reflection_max_edits=3,
        )
        self.repos = MemoryRepositories()
        self.runtime: GraphRuntime = build_graph_runtime(self.settings)
        self.runtime.audit_repositories = self.repos
        self.dot: Dot = create_dot(self.repos, self.runtime, "research-analyst", "owner")
        self.model = ScriptedChatModel(script=[], structured_script=structured)
        self.redactor = Redactor(secrets)
        self.files = MemoryFiles(self.runtime.store, self.dot.dot_id)

    def shortened(self, count: int = 3, at: datetime = NOW - timedelta(hours=3)) -> list[Episode]:
        """Edit episodes in which the human cut a long email body short."""
        return [
            self.repos.insert_episode(
                Episode(
                    0,
                    self.dot.dot_id,
                    at,
                    "[web] Email Sam the brief",
                    {"approval_id": f"card-{n}", "tool": "send_email", "args": {"to": "sam@example.com", "body": LONG}},
                    "edit",
                    {
                        "decided_by": "reviewer",
                        "decision": {"type": "edit", "edited_args": {"to": "sam@example.com", "body": SHORT}},
                        "execution": "not_yet_resumed",
                    },
                )
            )
            for n in range(count)
        ]

    def reflect(self, now: datetime = NOW) -> Any:
        return run_reflection(
            self.repos, self.runtime.store, self.dot, SCHEDULE, self.model, self.settings, self.redactor, now
        )

    def cursor(self) -> int | None:
        item = self.runtime.store.get((self.dot.dot_id, "reflection"), "/cursor")
        return int(item.value["last_episode_id"]) if item is not None else None


def edit(path: str, find: str, replace: str, ids: list[int], rationale: str = "The user shortens emails.") -> Any:
    return {"path": path, "find": find, "replace": replace, "rationale": rationale, "episode_ids": ids}


def test_a_preference_drawn_from_edits_is_proposed_not_applied(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [])
    episodes = rig.shortened()
    ids = [episode.id for episode in episodes]
    rig.model.structured_script = [
        {"edits": [edit(AGENTS_PATH, "", "# Preferences\n\n- Keep emails under 60 words.\n", ids)]}
    ]
    before = rig.files.listing()

    result = rig.reflect()

    assert [e.path for e in result.edits] == [AGENTS_PATH] and result.dropped == ()
    (version,) = rig.repos.list_memory_versions(rig.dot.dot_id)
    assert version.status == "proposed"
    assert version.episodes == ids
    assert version.diff == (
        "--- /dev/null\n+++ /memories/AGENTS.md\n@@ -0,0 +1,3 @@\n+# Preferences\n+\n+- Keep emails under 60 words.\n"
    )
    assert version.detail == {
        "path": AGENTS_PATH,
        "find": "",
        "replace": "# Preferences\n\n- Keep emails under 60 words.\n",
        "rationale": "The user shortens emails.",
        "base_sha256": None,
        "schedule": "reflection",
    }
    # Nothing is applied before the gate.
    assert rig.files.listing() == before and rig.files.read(AGENTS_PATH) is None
    assert rig.cursor() == ids[-1]

    (seen,) = rig.model.structured_seen
    system, human = seen
    assert isinstance(system, SystemMessage) and isinstance(human, HumanMessage)
    # Episode content reaches the model only as data, without the ids that locate state.
    assert SHORT not in str(system.content)
    data = json.loads(str(human.content))
    assert data["objective"] == "Learn email style."
    assert [e["id"] for e in data["episodes"]] == ids
    assert data["episodes"][0]["outcome"] == {
        "decision": {"type": "edit", "edited_args": {"to": "sam@example.com", "body": SHORT}}
    }
    assert data["episodes"][0]["proposal"]["tool"] == "send_email"
    assert SKILL in data["files"]


def test_an_edit_to_an_existing_skill_records_its_base(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [])
    ids = [episode.id for episode in rig.shortened(1)]
    current = rig.files.read(SKILL)
    assert current is not None
    rig.model.structured_script = [{"edits": [edit(SKILL, "- Keep it short.", "- Keep it under 60 words.", ids)]}]
    (proposed,) = rig.reflect().edits
    assert proposed.before == current
    assert proposed.after == current.replace("- Keep it short.", "- Keep it under 60 words.")
    (version,) = rig.repos.list_memory_versions(rig.dot.dot_id)
    assert proposed.base_sha256 is not None
    assert version.detail["base_sha256"] == proposed.base_sha256
    assert "\n-- Keep it short. The person" in version.diff
    assert "\n+- Keep it under 60 words. The person" in version.diff
    assert rig.files.read(SKILL) == current


@pytest.mark.parametrize(
    ("proposal", "reason"),
    [
        (lambda ids: edit("/memories/notes.md", "", "x", ids), "path is not AGENTS.md, a skill or a wiki page"),
        (
            lambda ids: edit("/wiki/../memories/AGENTS.md", "", "x", ids),
            "path is not AGENTS.md, a skill or a wiki page",
        ),
        (lambda ids: edit("/memories/skills/new-skill/SKILL.md", "", "x", ids), "skill does not exist"),
        (lambda ids: edit("/wiki/x.md", "", "x", [ids[0], 9_999]), "cites episodes this reflection was not given"),
        (lambda ids: edit(SKILL, "", "x", ids), "empty find on a file that exists"),
        (lambda ids: edit("/wiki/new-page.md", "anything", "x", ids), "file does not exist"),
        (lambda ids: edit(SKILL, "not in the file", "x", ids), "find matched 0 times"),
        (lambda ids: edit(SKILL, "e", "x", ids), "find matched"),
        (lambda ids: edit(SKILL, "- Keep it short.", "- Keep it short.", ids), "no change"),
        (lambda ids: edit(AGENTS_PATH, "PAD", "x" * 4_000, ids), "file would exceed 8000 characters"),
        (lambda ids: edit("/wiki/sam.md", "", "Use token sk-live-123 for Sam.", ids), "contains a secret"),
        (
            lambda ids: edit(SKILL, "---\nname: email-drafting", "name: email-drafting", ids),
            "SKILL.md would no longer parse",
        ),
    ],
)
def test_each_check_drops_an_edit_with_its_reason(tmp_path: Path, proposal: Any, reason: str) -> None:
    rig = Rig(tmp_path, [], secrets=("sk-live-123",))
    ids = [episode.id for episode in rig.shortened(2)]
    # Near AGENTS.md's 8000-character cap, so one more edit is too many.
    rig.runtime.store.put(
        (rig.dot.dot_id, "memories"), "/AGENTS.md", dict(create_file_data("PAD\n" + "p" * 5_000)), index=False
    )
    rig.model.structured_script = [{"edits": [proposal(ids)]}]
    result = rig.reflect()
    assert result.edits == ()
    (dropped,) = result.dropped
    assert dropped.reason.startswith(reason)
    assert rig.repos.list_memory_versions(rig.dot.dot_id) == []
    # A run whose edits were all dropped still consumed its episodes.
    assert rig.cursor() == ids[-1]


def test_an_edit_a_human_undid_is_not_proposed_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [])
    ids = [e.id for e in rig.shortened()]
    undone = {"path": AGENTS_PATH, "find": "", "replace": "- Keep emails short.\n", "rationale": "r"}
    rig.repos.insert_memory_version(MemoryVersion(0, rig.dot.dot_id, NOW, "", ids, "rolled_back", undone))
    rig.model.structured_script = [
        {
            "edits": [
                edit(AGENTS_PATH, "", "- Keep emails short.\n", ids),
                edit(AGENTS_PATH, "", "- Sign as Ada.\n", ids),
            ]
        }
    ]

    result = rig.reflect()

    assert [d.reason for d in result.dropped] == ["a human undid this edit"]
    (proposed,) = [v for v in rig.repos.list_memory_versions(rig.dot.dot_id) if v.status == "proposed"]
    assert proposed.detail["replace"] == "- Sign as Ada.\n"
    data = json.loads(str(rig.model.structured_seen[0][1].content))
    assert data["undone"] == [{"path": AGENTS_PATH, "replace": "- Keep emails short."}]


def test_edits_past_the_cap_are_dropped(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [])
    ids = [episode.id for episode in rig.shortened(1)]
    pages = [edit(f"/wiki/page-{n}.md", "", f"Page {n}\n", ids) for n in range(5)]
    rig.model.structured_script = [{"edits": pages}]
    result = rig.reflect()
    assert [e.path for e in result.edits] == ["/wiki/page-0.md", "/wiki/page-1.md", "/wiki/page-2.md"]
    assert [d.reason for d in result.dropped] == ["over the cap of 3 edits"] * 2
    assert len(rig.repos.list_memory_versions(rig.dot.dot_id)) == 3


def test_the_cursor_drains_episodes_and_skips_ones_still_settling(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [{"edits": []}, {"edits": []}])
    old = rig.shortened(2)
    fresh = rig.shortened(1, at=NOW - timedelta(seconds=10))

    rig.reflect()
    first = json.loads(str(rig.model.structured_seen[0][1].content))
    assert [e["id"] for e in first["episodes"]] == [e.id for e in old]
    assert rig.cursor() == old[-1].id

    rig.reflect(now=NOW + timedelta(minutes=5))
    second = json.loads(str(rig.model.structured_seen[1][1].content))
    assert [e["id"] for e in second["episodes"]] == [fresh[0].id]

    # Nothing new: no model call at all.
    rig.reflect(now=NOW + timedelta(minutes=10))
    assert len(rig.model.structured_seen) == 2


def test_a_model_failure_writes_nothing_and_keeps_the_cursor(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [RuntimeError("model unavailable")])
    rig.shortened(2)
    with pytest.raises(RuntimeError, match="model unavailable"):
        rig.reflect()
    assert rig.repos.list_memory_versions(rig.dot.dot_id) == []
    assert rig.cursor() is None


def test_the_worker_runs_a_reflection_row_without_an_agent(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [])
    ids = [episode.id for episode in rig.shortened(2, at=datetime.now(UTC) - timedelta(hours=1))]
    rig.model.structured_script = [{"edits": [edit(AGENTS_PATH, "", "- Keep emails short.\n", ids)]}]
    graph = build_dot_agent(rig.dot, "chat", settings=rig.settings, runtime=rig.runtime, model=rig.model)
    config = {"configurable": {"thread_id": rig.dot.thread_id}}
    checkpoints = len(list(graph.get_state_history(config)))

    row = trigger(rig.repos, rig.dot, "reflection", datetime.now(UTC))
    assert row is not None and row.profile == REFLECTION_PROFILE
    events = InMemoryEventChannel()
    run_agent_turn(rig.dot, row.profile, [row], events, settings=rig.settings, runtime=rig.runtime, model=rig.model)

    assert rig.model.calls == 0  # no supervisor turn
    assert len(list(graph.get_state_history(config))) == checkpoints
    # The gate runs next. These seeded episodes have no checkpoint, so the edit is held for a human.
    assert [v.status for v in rig.repos.list_memory_versions(rig.dot.dot_id)] == ["needs_review"]
    [event] = events.events
    assert event.kind == "memory" and event.detail["proposed"] == 1
    assert event.detail["judged"] == [v.id for v in rig.repos.list_memory_versions(rig.dot.dot_id)]

    rig.repos.update_inbox(replace(rig.repos.get_inbox(row.id), done_at=datetime.now(UTC)))
    rig.model.structured_script.append(RuntimeError("model unavailable"))
    rig.shortened(1, at=datetime.now(UTC) - timedelta(hours=1))
    again = trigger(rig.repos, rig.dot, "reflection", datetime.now(UTC) + timedelta(minutes=1))
    assert again is not None
    with pytest.raises(RuntimeError, match="model unavailable"):
        run_agent_turn(
            rig.dot, again.profile, [again], events, settings=rig.settings, runtime=rig.runtime, model=rig.model
        )


def test_reflection_schedules_take_no_profile_or_budget() -> None:
    with pytest.raises(ValidationError, match="takes no profile"):
        Schedule(name="r", kind="reflection", cron="0 2 * * *", prompt="p", profile="chat")
    with pytest.raises(ValidationError, match="needs a profile"):
        Schedule(name="s", cron="0 2 * * *", prompt="p")
    with pytest.raises(ValidationError, match="one model call"):
        Schedule(name="r", kind="reflection", cron="0 2 * * *", prompt="p", max_model_calls=3)
    pack = load_pack(REPO_ROOT / "packs/research-analyst").pack
    assert pack.schedule("reflection").inbox_profile == REFLECTION_PROFILE
