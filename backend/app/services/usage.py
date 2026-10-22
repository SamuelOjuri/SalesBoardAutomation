"""Shared Monday counters and structured, payload-free usage logs."""

import logging
from collections.abc import Callable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import MondayUsage

logger = logging.getLogger(__name__)


def record_usage(session: Session, operation: str, metric: str, count: int = 1) -> None:
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(MondayUsage).values(operation=operation, metric=metric, count=count)
    session.execute(statement.on_conflict_do_update(
        index_elements=["operation", "metric"],
        set_={"count": MondayUsage.count + count},
    ))


class UsageRecorder:
    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        self.session_factory = session_factory

    def record(self, operation: str, metric: str) -> None:
        logger.info("monday_usage operation=%s metric=%s count=1", operation, metric)
        if self.session_factory is not None:
            try:
                with self.session_factory() as session:
                    record_usage(session, operation, metric)
                    session.commit()
            except Exception as error:
                # Observability must never turn a successful write into a retry.
                logger.warning("could not persist Monday usage counters error_type=%s", type(error).__name__)
