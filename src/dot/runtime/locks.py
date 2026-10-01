"""Per-dot advisory locks.

The lock is held on a checked-out connection for the whole agent turn. A
transaction-scoped lock would drop when the claim commits, which is before
the agent runs. Closing the connection releases the lock, including on crash.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool


class DotLocks:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._held: dict[str, Connection[Any]] = {}

    def try_acquire(self, dot_id: str) -> bool:
        if dot_id in self._held:
            return True
        conn = self._pool.getconn()
        try:
            conn.autocommit = True
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (dot_id,),
            ).fetchone()
            locked = bool(row is not None and row[0])
        except Exception:
            self._put_back(conn)
            raise
        if not locked:
            self._put_back(conn)
            return False
        self._held[dot_id] = conn
        return True

    def release(self, dot_id: str) -> None:
        conn = self._held.pop(dot_id)
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (dot_id,))
        finally:
            self._put_back(conn)

    def _put_back(self, conn: Connection[Any]) -> None:
        conn.autocommit = False
        self._pool.putconn(conn)
