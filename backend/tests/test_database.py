from sqlalchemy import text

from app.database import (
    create_database_engine,
    create_session_factory,
    session_scope,
)


def test_session_scope_commits_work() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    session_factory = create_session_factory(engine)

    with session_scope(session_factory) as session:
        assert session.scalar(text("SELECT 1")) == 1

    engine.dispose()