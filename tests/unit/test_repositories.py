from __future__ import annotations

from dot.persistence.db import open_repositories
from tests.support.repository_contract import exercise


def test_memory_repositories_match_the_contract() -> None:
    repos = open_repositories(None)
    exercise(repos)
    repos.close()
