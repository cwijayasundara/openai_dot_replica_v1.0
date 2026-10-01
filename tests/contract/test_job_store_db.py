"""The Postgres job store: the shared contract plus claims across runners."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator

import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from dot.jobs.store import PostgresJobStore
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import Approval, ApprovalConflict, PostgresRepositories, make_pool, migrate
from dot.safety.approvals import ReviewDecision, decide
from tests.support.job_store_contract import exercise, seed

pytestmark = pytest.mark.db


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    url = os.environ.get("DOT_DATABASE_URL")
    if not url:
        pytest.skip("DOT_DATABASE_URL is not set")
    opened = make_pool(url)
    try:
        migrate(opened)
        with opened.connection() as conn:
            conn.execute(
                "TRUNCATE users, dots, inbox, jobs, approvals, audit_log, findings, episodes,"
                " memory_versions, channel_bindings RESTART IDENTITY CASCADE"
            )
    except Exception:
        opened.close()
        raise
    yield opened
    opened.close()


def _rows(pool: ConnectionPool, dot_id: str) -> list[tuple[str, str, dict]]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT source, profile, payload FROM inbox WHERE dot_id = %s ORDER BY id", (dot_id,)
        ).fetchall()
    return [(row["source"], row["profile"], row["payload"]) for row in rows]


def test_postgres_job_store_matches_the_contract(pool: ConnectionPool) -> None:
    exercise(PostgresJobStore(pool), PostgresRepositories(pool), lambda dot_id: _rows(pool, dot_id))


def test_runners_in_other_processes_claim_distinct_jobs(pool: ConnectionPool) -> None:
    repos = PostgresRepositories(pool)
    seed(repos)
    first, second = PostgresJobStore(pool), PostgresJobStore(pool)
    ids = {first.create("d1", "researcher", f"job {n}", profile="chat", origin={}).job_id for n in range(6)}
    claimed: list[str] = []
    holders: dict[str, PostgresJobStore] = {}
    guard = threading.Lock()
    start = threading.Barrier(4)

    def take(store: PostgresJobStore) -> None:
        start.wait()
        while (job := store.claim()) is not None:
            with guard:
                claimed.append(job.job_id)
                holders[job.job_id] = store

    threads = [threading.Thread(target=take, args=(store,)) for store in (first, second, first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(claimed) == sorted(ids)
    # Held claims block other runners until released.
    assert second.claim() is None and first.claim() is None
    for job_id, store in holders.items():
        store.release(job_id)


def _paused_job(
    pool: ConnectionPool, repos: PostgresRepositories, cards: int
) -> tuple[PostgresJobStore, str, list[str]]:
    store = PostgresJobStore(pool)
    job = store.create("d1", "researcher", "look it up", profile="chat", origin={"instruction": "Research it."})
    claimed = store.claim()
    assert claimed is not None and claimed.job_id == job.job_id
    run_ref = json.dumps(
        {
            "thread_id": job.thread_id,
            "checkpoint_id": "cp-1",
            "profile": "chat",
            "interrupts": [{"interrupt_id": "i1", "approval_ids": [f"card-{n}" for n in range(cards)]}],
            "task": "Research it.",
            "turn_id": "turn-1",
            "job_id": job.job_id,
        },
        sort_keys=True,
    )
    approvals = [Approval(f"card-{n}", "d1", run_ref, "web_search", {"query": str(n)}, "pending") for n in range(cards)]
    assert repos.pause_job_for_approvals(job.job_id, run_ref, approvals) == approvals
    store.release(job.job_id)
    assert store.current(job.job_id).status == "paused"
    assert store.claim() is None
    return store, job.job_id, [a.approval_id for a in approvals]


def _approvers(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = load_pack(REPO_ROOT / "packs/research-analyst")
    loaded.policy.approvers = ["reviewer"]
    monkeypatch.setattr("dot.safety.approvals.load_pack", lambda _: loaded)


def test_concurrent_last_decisions_queue_a_paused_job_once(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos = PostgresRepositories(pool)
    seed(repos)
    _approvers(monkeypatch)
    store, job_id, cards = _paused_job(pool, repos, 2)
    start = threading.Barrier(2)
    errors: list[Exception] = []

    def approve(card: str) -> None:
        start.wait()
        try:
            decide(repos, card, "reviewer", ReviewDecision(type="approve"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=approve, args=(card,)) for card in cards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert store.current(job_id).status == "queued"
    assert {repos.get_approval(card).status for card in cards} == {"approve"}
    with pool.connection() as conn:
        episodes = conn.execute("SELECT count(*) FROM episodes").fetchone()
        inbox = conn.execute("SELECT count(*) FROM inbox").fetchone()
    assert episodes is not None and episodes[0] == 2
    assert inbox is not None and inbox[0] == 0  # a job resumes by being claimed, not via the inbox
    assert repos.get_dot("d1").status == "active"
    claimed = store.claim()
    assert claimed is not None and claimed.job_id == job_id
    store.release(job_id)


def test_cancel_and_decision_race_leaves_one_consistent_outcome(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos = PostgresRepositories(pool)
    seed(repos)
    _approvers(monkeypatch)
    store, job_id, [card] = _paused_job(pool, repos, 1)
    start = threading.Barrier(2)
    conflicts: list[Exception] = []

    def approve() -> None:
        start.wait()
        try:
            decide(repos, card, "reviewer", ReviewDecision(type="approve"))
        except ApprovalConflict as exc:
            conflicts.append(exc)

    def cancel() -> None:
        start.wait()
        store.cancel("d1", job_id)

    threads = [threading.Thread(target=approve), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    job, decided = store.current(job_id), repos.get_approval(card)
    if conflicts:  # cancel won: the card is closed and nothing runs
        assert job.status == "cancelled" and decided.status == "cancelled"
    else:  # the decision won, then cancel stopped the queued job
        assert job.status == "cancelled" and decided.status == "approve"
    assert store.claim() is None
