"""Durable delivery to external channels.

The worker writes replies and approval cards here; a channel process posts
them. ``NOTIFY`` is not enough: it is lost while that process is down and it
truncates large payloads.

Delivery is at least once and in order per channel. One delivery loop holds
the channel; a failing row is retried in place, which holds back later rows,
until it is parked after ``PARK_AFTER`` attempts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Lock
from typing import Any, Protocol

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from dot.persistence.db import Json, MemoryRepositories

PARK_AFTER = 5


@dataclass(frozen=True)
class OutboxItem:
    id: int
    dot_id: str
    channel: str
    kind: str
    target: Json
    body: Json
    attempts: int = 0
    last_error: str | None = None
    delivered: bool = False
    parked: bool = False


@dataclass(frozen=True)
class CardChange:
    """A posted approval card whose decision differs from what it shows."""

    approval_id: str
    ref: Json
    status: str
    decided_by: str | None


class Outbox(Protocol):
    def add(self, dot_id: str, channel: str, kind: str, target: Json, body: Json) -> None: ...

    def next(self, channel: str) -> OutboxItem | None:
        """The oldest undelivered, unparked item for this channel."""
        ...

    def delivered(self, item_id: int) -> None: ...

    def failed(self, item_id: int, error: str, *, park: bool = False) -> bool:
        """Record a failed attempt. True when the item is now parked.

        ``park`` parks at once, for errors a retry cannot fix.
        """
        ...

    def first_seen(self, channel: str, event_key: str) -> bool:
        """Record an inbound event key. False when it was already recorded."""
        ...

    def record_card(self, approval_id: str, channel: str, ref: Json) -> None: ...

    def card_changes(self, channel: str) -> list[CardChange]: ...

    def card_shown(self, approval_id: str, channel: str, status: str) -> None: ...


class MemoryOutbox:
    def __init__(self, repos: MemoryRepositories) -> None:
        self._repos = repos
        self._lock = Lock()
        self.items: dict[int, OutboxItem] = {}
        self._seen: set[tuple[str, str]] = set()
        self._cards: dict[tuple[str, str], tuple[Json, str]] = {}
        self._next_id = 0

    def add(self, dot_id: str, channel: str, kind: str, target: Json, body: Json) -> None:
        with self._lock:
            self._next_id += 1
            self.items[self._next_id] = OutboxItem(self._next_id, dot_id, channel, kind, dict(target), dict(body))

    def next(self, channel: str) -> OutboxItem | None:
        with self._lock:
            waiting = [i for i in self.items.values() if i.channel == channel and not i.delivered and not i.parked]
            return min(waiting, key=lambda item: item.id) if waiting else None

    def delivered(self, item_id: int) -> None:
        with self._lock:
            self.items[item_id] = replace(self.items[item_id], delivered=True)

    def failed(self, item_id: int, error: str, *, park: bool = False) -> bool:
        with self._lock:
            item = self.items[item_id]
            attempts = item.attempts + 1
            parked = park or attempts >= PARK_AFTER
            self.items[item_id] = replace(item, attempts=attempts, last_error=error, parked=parked)
            return parked

    def first_seen(self, channel: str, event_key: str) -> bool:
        with self._lock:
            if (channel, event_key) in self._seen:
                return False
            self._seen.add((channel, event_key))
            return True

    def record_card(self, approval_id: str, channel: str, ref: Json) -> None:
        with self._lock:
            self._cards[(approval_id, channel)] = (dict(ref), "pending")

    def card_changes(self, channel: str) -> list[CardChange]:
        with self._lock:
            cards = [(key[0], ref, shown) for key, (ref, shown) in self._cards.items() if key[1] == channel]
        changes = []
        for approval_id, ref, shown in cards:
            approval = self._repos.get_approval(approval_id)
            if approval.status != shown:
                changes.append(CardChange(approval_id, ref, approval.status, approval.decided_by))
        return changes

    def card_shown(self, approval_id: str, channel: str, status: str) -> None:
        with self._lock:
            ref, _ = self._cards[(approval_id, channel)]
            self._cards[(approval_id, channel)] = (ref, status)


class PostgresOutbox:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def add(self, dot_id: str, channel: str, kind: str, target: Json, body: Json) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO outbox (dot_id, channel, kind, target, body) VALUES (%s, %s, %s, %s, %s)",
                (dot_id, channel, kind, Jsonb(target), Jsonb(body)),
            )

    def next(self, channel: str) -> OutboxItem | None:
        row = self._row(
            "SELECT * FROM outbox WHERE channel = %s AND delivered_at IS NULL AND parked_at IS NULL"
            " ORDER BY id LIMIT 1",
            (channel,),
        )
        return _item(row) if row is not None else None

    def delivered(self, item_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE outbox SET delivered_at = now() WHERE id = %s", (item_id,))

    def failed(self, item_id: int, error: str, *, park: bool = False) -> bool:
        row = self._row(
            "UPDATE outbox SET attempts = attempts + 1, last_error = %s,"
            " parked_at = CASE WHEN %s OR attempts + 1 >= %s THEN now() ELSE NULL END"
            " WHERE id = %s RETURNING parked_at",
            (error[:2000], park, PARK_AFTER, item_id),
        )
        return row is not None and row["parked_at"] is not None

    def first_seen(self, channel: str, event_key: str) -> bool:
        try:
            with self._pool.connection() as conn:
                conn.execute("INSERT INTO channel_events (channel, event_key) VALUES (%s, %s)", (channel, event_key))
        except UniqueViolation:
            return False
        return True

    def record_card(self, approval_id: str, channel: str, ref: Json) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO approval_posts (approval_id, channel, ref, shown_status) VALUES (%s, %s, %s, 'pending')"
                " ON CONFLICT (approval_id, channel) DO UPDATE SET ref = EXCLUDED.ref",
                (approval_id, channel, Jsonb(ref)),
            )

    def card_changes(self, channel: str) -> list[CardChange]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT p.approval_id, p.ref, a.status, a.decided_by FROM approval_posts p"
                " JOIN approvals a ON a.approval_id = p.approval_id"
                " WHERE p.channel = %s AND a.status <> p.shown_status ORDER BY p.approval_id",
                (channel,),
            ).fetchall()
        return [CardChange(r["approval_id"], dict(r["ref"]), r["status"], r["decided_by"]) for r in rows]

    def card_shown(self, approval_id: str, channel: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE approval_posts SET shown_status = %s WHERE approval_id = %s AND channel = %s",
                (status, approval_id, channel),
            )

    def _row(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(sql, params).fetchone()


def _item(row: dict[str, Any]) -> OutboxItem:
    return OutboxItem(
        id=int(row["id"]),
        dot_id=row["dot_id"],
        channel=row["channel"],
        kind=row["kind"],
        target=dict(row["target"]),
        body=dict(row["body"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        delivered=row["delivered_at"] is not None,
        parked=row["parked_at"] is not None,
    )
