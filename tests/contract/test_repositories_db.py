from __future__ import annotations

import os

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore

from dot.assembly import build_graph_runtime
from dot.config import Settings
from dot.persistence.db import make_pool, migrate, open_repositories
from tests.support.repository_contract import exercise

pytestmark = pytest.mark.db


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("DOT_DATABASE_URL")
    if not url:
        pytest.skip("DOT_DATABASE_URL is not set")
    pool = make_pool(url)
    try:
        migrate(pool)
        with pool.connection() as conn:
            conn.execute(
                "TRUNCATE users, dots, inbox, jobs, approvals, audit_log, findings, episodes,"
                " memory_versions, channel_bindings RESTART IDENTITY CASCADE"
            )
    finally:
        pool.close()
    return url


def test_postgres_repositories_match_the_contract(database_url: str) -> None:
    repos = open_repositories(database_url)
    try:
        exercise(repos)
    finally:
        repos.close()


def test_postgres_graph_runtime(database_url: str) -> None:
    runtime = build_graph_runtime(Settings(_env_file=None, database_url=database_url))  # type: ignore[call-arg]
    try:
        assert isinstance(runtime.checkpointer, PostgresSaver)
        assert isinstance(runtime.store, PostgresStore)
    finally:
        runtime.close()


def test_migrations_are_idempotent(database_url: str) -> None:
    pool = make_pool(database_url)
    try:
        migrate(pool)
        migrate(pool)
    finally:
        pool.close()
