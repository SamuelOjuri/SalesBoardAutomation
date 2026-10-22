"""Durable complete-board snapshots with one refresh at a time per source/board."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, TypeVar

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import ReferenceIndexSnapshot
from app.services.usage import record_usage


IndexT = TypeVar("IndexT")
IndexKind = Literal["accounts", "contacts"]
# Bump when parsing rules or the persisted record format change. Old workers
# must never interpret a snapshot produced under incompatible semantics.
REFERENCE_INDEX_FORMAT_VERSION = 1


def reference_cache_scope(*, api_url: str, api_version: str, access_token: str) -> str:
    """Isolate sources and permission scopes without persisting credentials."""

    return _digest([api_url.rstrip("/"), api_version, access_token])


class SharedIndexCache:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        source_scope: str,
    ) -> None:
        self._session_factory = session_factory
        self._source_scope = source_scope

    def load_index(
        self,
        *,
        index_kind: IndexKind,
        board_id: int,
        cache_ttl_seconds: float,
        fetch_index: Callable[[], IndexT],
        adapter: TypeAdapter[IndexT],
        force_refresh: bool = False,
    ) -> IndexT:
        cache_key = self._cache_key(index_kind, board_id)
        # This is independent of job/heartbeat transactions. The row lock lasts
        # through every Monday page and the atomic replacement of the snapshot.
        # A disconnect/crash rolls back and releases the lock automatically.
        with self._session_factory.begin() as session:
            if not force_refresh:
                snapshot = session.get(ReferenceIndexSnapshot, cache_key)
                if self._fresh(snapshot, self._now(session), cache_ttl_seconds):
                    record_usage(session, index_kind, "cache_hits")
                    return adapter.validate_json(
                        json.dumps(snapshot.payload_json), strict=True
                    )

            snapshot = self._lock_snapshot(session, cache_key, index_kind, board_id)
            # Another worker may have completed the refresh while we waited.
            # Read DB wall time after acquiring the lock, not transaction time.
            if not force_refresh and self._fresh(
                snapshot, self._now(session), cache_ttl_seconds
            ):
                record_usage(session, index_kind, "cache_hits")
                return adapter.validate_json(
                    json.dumps(snapshot.payload_json), strict=True
                )

            record_usage(session, index_kind, "cache_misses")
            index = fetch_index()
            snapshot.payload_json = json.loads(adapter.dump_json(index))
            snapshot.refreshed_at = self._now(session)
            return index

    def clear(self, *, index_kind: IndexKind, board_id: int) -> None:
        """Invalidate the shared snapshot, including readers in other workers."""

        cache_key = self._cache_key(index_kind, board_id)
        with self._session_factory.begin() as session:
            snapshot = self._lock_snapshot(session, cache_key, index_kind, board_id)
            snapshot.payload_json = None
            snapshot.refreshed_at = None

    def _cache_key(self, index_kind: IndexKind, board_id: int) -> str:
        return _digest(
            [REFERENCE_INDEX_FORMAT_VERSION, self._source_scope, index_kind, str(board_id)]
        )

    @staticmethod
    def _lock_snapshot(
        session: Session, cache_key: str, index_kind: IndexKind, board_id: int
    ) -> ReferenceIndexSnapshot:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            insert = postgresql_insert
        elif dialect == "sqlite":
            # Local tests only: the upsert obtains SQLite's write lock. Runtime
            # uses PostgreSQL row locks, so unrelated boards refresh independently.
            insert = sqlite_insert
        else:
            raise RuntimeError("shared reference indexes require PostgreSQL")
        session.execute(
            insert(ReferenceIndexSnapshot)
            .values(cache_key=cache_key, index_kind=index_kind, board_id=str(board_id))
            .on_conflict_do_nothing(index_elements=["cache_key"])
        )
        return session.scalars(
            select(ReferenceIndexSnapshot)
            .where(ReferenceIndexSnapshot.cache_key == cache_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    @staticmethod
    def _now(session: Session) -> datetime:
        if session.get_bind().dialect.name == "postgresql":
            return session.scalar(select(func.clock_timestamp()))
        return datetime.now(timezone.utc)

    @staticmethod
    def _fresh(
        snapshot: ReferenceIndexSnapshot | None,
        now: datetime,
        cache_ttl_seconds: float,
    ) -> bool:
        if snapshot is None or snapshot.payload_json is None or snapshot.refreshed_at is None:
            return False
        refreshed_at = snapshot.refreshed_at
        if refreshed_at.tzinfo is None:  # SQLite does not retain timezones.
            refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
        age_seconds = (now - refreshed_at).total_seconds()
        return 0 <= age_seconds < cache_ttl_seconds


def _digest(values: list[object]) -> str:
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
