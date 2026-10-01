from __future__ import annotations

from dot.jobs.store import MemoryJobStore
from dot.persistence.db import MemoryRepositories
from tests.support.job_store_contract import exercise


def test_memory_job_store_matches_the_contract() -> None:
    repos = MemoryRepositories()

    def rows(dot_id: str) -> list[tuple[str, str, dict]]:
        return [(m.source, m.profile, m.payload) for m in repos.inbox.values() if m.dot_id == dot_id]

    exercise(MemoryJobStore(repos), repos, rows)
