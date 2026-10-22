"""Durable cooldowns shared by workers, webhooks, and operator clients."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import MondayCooldown
from app.monday_errors import MondayAPIError, MondayCooldownError


def monday_cooldown_scope(*, api_url: str, access_token: str) -> str:
    # API versions share quota. Never persist the credential itself.
    source = json.dumps([api_url.rstrip("/"), access_token], separators=(",", ":"))
    return hashlib.sha256(source.encode()).hexdigest()


class SharedMondayCooldown:
    def __init__(self, *, session_factory: sessionmaker[Session], source_scope: str) -> None:
        self._session_factory = session_factory
        self._source_scope = source_scope

    @classmethod
    def from_settings(
        cls, settings: Settings, session_factory: sessionmaker[Session]
    ) -> SharedMondayCooldown:
        return cls(
            session_factory=session_factory,
            source_scope=monday_cooldown_scope(
                api_url=settings.monday_api_url,
                access_token=settings.monday_ingestion_access_token.get_secret_value(),
            ),
        )

    def active_error(self, *, now: datetime) -> MondayCooldownError | None:
        with self._session_factory() as session:
            state = session.get(MondayCooldown, self._source_scope)
            if state is None:
                return None
            retry_at = state.retry_at
            if retry_at.tzinfo is None:  # SQLite tests.
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            if retry_at <= now:
                return None
            return MondayCooldownError(
                "Monday requests are cooling down",
                code=state.error_code,
                status_code=state.status_code,
                retry_at=retry_at,
            )

    def record(self, error: MondayAPIError) -> None:
        if error.retry_at is None:
            return
        # Independent commit survives a rollback of processing/board refresh.
        # Atomic max prevents a concurrent shorter cooldown replacing a longer one.
        with self._session_factory.begin() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                insert = postgresql_insert
            elif dialect == "sqlite":
                insert = sqlite_insert
            else:
                raise RuntimeError("Monday cooldowns require PostgreSQL")
            statement = insert(MondayCooldown).values(
                source_scope=self._source_scope,
                retry_at=error.retry_at,
                error_code=error.code or "monday_temporary_failure",
                status_code=error.status_code,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["source_scope"],
                    set_={
                        "retry_at": statement.excluded.retry_at,
                        "error_code": statement.excluded.error_code,
                        "status_code": statement.excluded.status_code,
                    },
                    where=MondayCooldown.retry_at < statement.excluded.retry_at,
                )
            )
