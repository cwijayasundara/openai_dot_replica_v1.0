"""One contract for the in-memory and Postgres job stores."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from dot.jobs.store import JobClosed, JobStore
from dot.persistence.db import Dot, NotFound, Repositories, User

WHEN = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
ORIGIN = {"source": "slack", "inbox_ids": [7], "instruction": "Research the topic"}


def seed(repos: Repositories) -> None:
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("d1", "u1", "research-analyst", "1", "thread-1", "active", WHEN))
    repos.create_dot(Dot("d2", "u1", "research-analyst", "1", "thread-2", "active", WHEN))


def exercise(store: JobStore, repos: Repositories, inbox_rows: Callable[[str], list[tuple[str, str, dict]]]) -> None:
    seed(repos)
    first = store.create("d1", "researcher", "look it up", profile="chat", origin=ORIGIN)
    assert first.status == "queued" and first.profile == "chat" and first.origin == ORIGIN
    assert first.thread_id.startswith("thread-1:") and first.thread_id != "thread-1"
    assert store.get("d1", first.job_id) == first
    with pytest.raises(NotFound):
        store.get("d2", first.job_id)
    with pytest.raises(NotFound):
        store.create("missing", "researcher", "x", profile="chat", origin={})

    # A queued job that is cancelled is never claimed.
    doomed = store.create("d1", "researcher", "never runs", profile="chat", origin={})
    assert store.cancel("d1", doomed.job_id).status == "cancelled"
    other = store.create("d2", "researcher", "other dot", profile="sweep", origin={})

    claimed = store.claim()
    assert claimed is not None and claimed.job_id == first.job_id
    assert claimed.status == "running" and claimed.started_at is not None
    second = store.claim()
    assert second is not None and second.job_id == other.job_id
    assert store.claim() is None

    assert [job.job_id for job in store.list_jobs("d1")] == [first.job_id, doomed.job_id]
    assert [job.job_id for job in store.list_jobs("d1", "running")] == [first.job_id]
    with pytest.raises(ValueError):
        store.list_jobs("d1", "bogus")  # type: ignore[arg-type]

    store.append_update("d1", first.job_id, "focus on 2026")
    updated = store.append_update("d1", first.job_id, "cite sources")
    assert [u["message"] for u in updated.updates] == ["focus on 2026", "cite sources"]
    with pytest.raises(NotFound):
        store.append_update("d2", first.job_id, "not yours")
    with pytest.raises(JobClosed):
        store.append_update("d1", doomed.job_id, "too late")

    notify = {"text": "done", "job_id": first.job_id}
    assert store.finish(first.job_id, "succeeded", result_ref="art_0", error=None, notify=notify)
    store.release(first.job_id)
    done = store.current(first.job_id)
    assert done.status == "succeeded" and done.result_ref == "art_0" and done.finished_at is not None
    assert inbox_rows("d1") == [("job_result", "chat", notify)]
    # Finished jobs stay finished: no second finish, cancel or update.
    assert not store.finish(first.job_id, "failed", result_ref=None, error="x", notify=notify)
    assert store.cancel("d1", first.job_id).status == "succeeded"
    with pytest.raises(JobClosed):
        store.append_update("d1", first.job_id, "after")

    # A cancel while running wins over the runner's finish, which posts nothing.
    assert store.cancel("d2", other.job_id).status == "cancelled"
    assert not store.finish(other.job_id, "succeeded", result_ref="art_1", error=None, notify=notify)
    store.release(other.job_id)
    assert store.current(other.job_id).status == "cancelled"
    assert inbox_rows("d2") == []

    # A running job whose runner let go without finishing is claimed again.
    orphan = store.create("d1", "researcher", "resume me", profile="chat", origin={})
    taken = store.claim()
    assert taken is not None and taken.job_id == orphan.job_id
    store.release(orphan.job_id)
    again = store.claim()
    assert again is not None and again.job_id == orphan.job_id
    assert again.started_at == taken.started_at
    store.release(orphan.job_id)
