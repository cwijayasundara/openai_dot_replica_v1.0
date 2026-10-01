from __future__ import annotations

from dot.channels.outbox import MemoryOutbox
from dot.persistence.db import MemoryRepositories
from tests.support.outbox_contract import exercise


def test_memory_outbox_matches_the_contract() -> None:
    repos = MemoryRepositories()
    exercise(MemoryOutbox(repos), repos)
