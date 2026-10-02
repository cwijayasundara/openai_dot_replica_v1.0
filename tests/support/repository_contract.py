"""One contract for the in-memory and Postgres repositories."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from dot.persistence.db import (
    AppendOnly,
    Approval,
    AuditEvent,
    ChannelBinding,
    Dot,
    Episode,
    Finding,
    InboxMessage,
    Job,
    MemoryVersion,
    NotFound,
    Repositories,
    User,
)

WHEN = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def exercise(repos: Repositories) -> None:
    repos.create_user(User("u1", "Ada", slack_user_id="U1", web_subject="ada@example"))
    assert repos.get_user("u1") == User("u1", "Ada", "U1", "ada@example")
    repos.update_user(User("u1", "Ada Lovelace", "U1", "ada@example"))
    assert repos.get_user("u1").display_name == "Ada Lovelace"
    assert repos.find_user_by_web_subject("ada@example").user_id == "u1"
    with pytest.raises(NotFound):
        repos.find_user_by_web_subject("nobody@example")

    dot = Dot("d1", "u1", "research-analyst", "1", "thread-1", "active", WHEN)
    repos.create_dot(dot)
    assert repos.get_dot("d1") == dot
    paused = Dot("d1", "u1", "research-analyst", "1", "thread-1", "paused", WHEN)
    repos.update_dot(paused)
    assert repos.get_dot("d1").status == "paused"

    inbox = repos.insert_inbox(InboxMessage(0, "d1", "web", {"text": "hello"}, "chat", WHEN))
    assert inbox.id > 0
    assert repos.get_inbox(inbox.id).payload == {"text": "hello"}
    claimed = InboxMessage(inbox.id, "d1", "web", {"text": "hello"}, "chat", WHEN, claimed_at=WHEN)
    repos.update_inbox(claimed)
    assert repos.get_inbox(inbox.id).claimed_at == WHEN

    job = Job("j1", "d1", "researcher", "look it up", "queued", "job-thread", [], WHEN)
    repos.create_job(job)
    assert repos.get_job("j1") == job
    done = Job("j1", "d1", "researcher", "look it up", "succeeded", "job-thread", [{"n": 1}], WHEN, "art-1", WHEN)
    repos.update_job(done)
    assert repos.get_job("j1").status == "succeeded"
    assert repos.get_job("j1").updates == [{"n": 1}]
    sourced = Job(
        "j2",
        "d1",
        "researcher",
        "dig",
        "failed",
        "job-thread-2",
        [],
        WHEN,
        None,
        WHEN,
        profile="sweep",
        origin={"source": "slack"},
        error="boom",
        started_at=WHEN,
    )
    repos.create_job(sourced)
    assert repos.get_job("j2") == sourced

    approval = Approval("a1", "d1", "run-1", "send_email", {"to": "sam"}, "pending")
    repos.create_approval(approval)
    assert repos.get_approval("a1") == approval
    decided = Approval("a1", "d1", "run-1", "send_email", {"to": "sam"}, "approved", "u1", WHEN, {"to": "sam"})
    repos.update_approval(decided)
    assert repos.get_approval("a1").status == "approved"
    assert repos.get_approval("a1").edit == {"to": "sam"}
    waiting = Approval("a2", "d1", "run-2", "send_email", {"to": "kim"}, "pending")
    repos.create_approval(waiting)
    cancelled = Approval("a0", "d1", "run-3", "send_email", {"to": "lee"}, "cancelled")
    repos.create_approval(cancelled)
    assert repos.list_dot_approvals("d1") == [waiting, repos.get_approval("a1"), cancelled]
    assert repos.list_dot_approvals("d1", "pending") == [waiting]

    event = repos.append_audit(
        AuditEvent(0, "d1", WHEN, "guardian", "verdict", tool="send_email", effect="external", decision="approve")
    )
    assert event.id > 0
    assert repos.get_audit(event.id).kind == "verdict"
    with pytest.raises(AppendOnly):
        repos.update_audit(event)
    next_event = repos.append_audit(
        AuditEvent(0, "d1", WHEN, "supervisor", "tool_call", detail={"turn_id": "turn-1", "nested": {"n": 1}})
    )
    assert repos.list_audit("d1", limit=1) == [event]
    assert repos.list_audit("d1", after_id=event.id) == [next_event]
    assert repos.list_audit("d1", turn_id="turn-1") == [next_event]
    assert repos.list_audit("d1", turn_id="other") == []
    fetched = repos.list_audit("d1", turn_id="turn-1")[0]
    assert fetched.detail is not None
    fetched.detail["nested"]["n"] = 99
    assert repos.get_audit(next_event.id).detail == {"turn_id": "turn-1", "nested": {"n": 1}}
    assert repos.list_audit("d1", actors=["guardian"]) == [event]
    assert repos.list_audit("d1", actors=[]) == []

    finding = repos.insert_finding(Finding(0, "d1", "sweep", "A filing", {"url": "https://example"}, 0.5, "open", WHEN))
    assert repos.get_finding(finding.id).title == "A filing"
    repos.update_finding(
        Finding(finding.id, "d1", "sweep", "A filing", {"url": "https://example"}, 0.5, "reported", WHEN)
    )
    assert repos.get_finding(finding.id).status == "reported"
    later = repos.insert_finding(Finding(0, "d1", "sweep", "Newer", {}, 0.9, "open", WHEN.replace(hour=13)))
    assert [f.id for f in repos.list_findings("d1")] == [later.id, finding.id]
    assert [f.id for f in repos.list_findings("d1", "open")] == [later.id]

    episode = repos.insert_episode(Episode(0, "d1", WHEN, "draft", {"len": 400}, "shorten", {"ok": True}))
    assert repos.get_episode(episode.id).human_action == "shorten"
    repos.update_episode(Episode(episode.id, "d1", WHEN, "draft", {"len": 80}, "shorten", {"ok": True}))
    assert repos.get_episode(episode.id).proposal == {"len": 80}

    second = repos.insert_episode(Episode(0, "d1", WHEN.replace(hour=13), "draft", {}, "approve", {}))
    third = repos.insert_episode(Episode(0, "d1", WHEN.replace(hour=14), "draft", {}, "reject", {}))
    assert [e.id for e in repos.list_episodes("d1")] == [episode.id, second.id, third.id]
    assert [e.id for e in repos.list_episodes("d1", after_id=episode.id, limit=1)] == [second.id]
    assert [e.id for e in repos.list_episodes("d1", before=WHEN.replace(hour=14))] == [episode.id, second.id]

    version = repos.insert_memory_version(
        MemoryVersion(0, "d1", WHEN, "--- a\n+++ b", [episode.id], "proposed", {"path": "/memories/AGENTS.md"})
    )
    assert repos.get_memory_version(version.id).episodes == [episode.id]
    assert repos.get_memory_version(version.id).detail == {"path": "/memories/AGENTS.md"}
    repos.update_memory_version(
        MemoryVersion(version.id, "d1", WHEN, "--- a\n+++ b", [episode.id], "accepted", {"reason": "replay"})
    )
    assert repos.get_memory_version(version.id).status == "accepted"
    assert repos.get_memory_version(version.id).detail == {"reason": "replay"}
    newer = repos.insert_memory_version(MemoryVersion(0, "d1", WHEN.replace(hour=13), "+x", [], "proposed"))
    assert [v.id for v in repos.list_memory_versions("d1")] == [newer.id, version.id]

    repos.bind_channel(ChannelBinding("d1", "slack", "D123"))
    assert repos.get_channel("d1", "slack").external_id == "D123"
    repos.update_channel(ChannelBinding("d1", "slack", "D456"))
    assert repos.get_channel("d1", "slack").external_id == "D456"

    # d1 is paused above; only active dots of the named pack are scheduled.
    repos.create_dot(Dot("d2", "u1", "research-analyst", "1", "thread-2", "active", WHEN))
    repos.create_dot(Dot("d3", "u1", "other-pack", "1", "thread-3", "active", WHEN))
    assert [d.dot_id for d in repos.list_active_dots("research-analyst")] == ["d2"]

    def run(slot: str, schedule: str = "sweep") -> InboxMessage:
        return InboxMessage(0, "d2", "schedule", {"schedule": schedule, "slot": slot, "text": "Sweep."}, "sweep", WHEN)

    first = repos.insert_schedule_run(run("2026-09-30T12:00"))
    assert first is not None and first.source == "schedule" and first.payload["slot"] == "2026-09-30T12:00"
    # A pending run absorbs the next firing, and another schedule is independent.
    assert repos.insert_schedule_run(run("2026-09-30T12:30")) is None
    assert repos.insert_schedule_run(run("2026-09-30T12:30", "digest")) is not None
    repos.update_inbox(replace(first, claimed_at=WHEN, done_at=WHEN))
    # A slot that already ran is not queued again, as when Cloud Scheduler retries.
    assert repos.insert_schedule_run(run("2026-09-30T12:00")) is None
    assert repos.insert_schedule_run(run("2026-09-30T12:30")) is not None
    with pytest.raises(ValueError):
        repos.insert_schedule_run(InboxMessage(0, "d2", "web", {"text": "hi"}, "chat", WHEN))
