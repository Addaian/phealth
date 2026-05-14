"""
Shared pytest fixtures.

  * ``client``     -- FastAPI TestClient; dependency-free, needs no database.
  * ``db_engine``  -- session-scoped engine against the configured database;
                      *skips* dependent tests if the DB is unreachable, so the
                      suite still runs green on a checkout with no Postgres up.
  * ``db_session`` -- function-scoped session wrapped in a transaction that is
                      rolled back after each test, so tests never pollute the
                      database or each other.

Database-backed tests assume the schema has been migrated
(`alembic upgrade head`); see documents/phase_1_implementation_plan.md §M5.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session

from app.main import app


@pytest.fixture
def client() -> TestClient:
    """A TestClient bound to the FastAPI app."""
    return TestClient(app)


@pytest.fixture(scope="session")
def db_engine():
    """Engine against the configured database.

    Skips (rather than fails) dependent tests if the database cannot be
    reached — keeps `pytest` green on a checkout with no running Postgres.
    """
    try:
        from app.db.session import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 -- any failure here means "skip DB tests"
        pytest.skip(
            f"database not reachable ({exc}); run "
            "`docker compose up -d postgres && alembic upgrade head`"
        )
    return engine


@pytest.fixture
def db_session(db_engine):
    """A session wrapped in a transaction that is rolled back after the test.

    Each test sees a clean database and leaves nothing behind — no test can
    pollute another or the developer's data.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        # A test that triggers an IntegrityError leaves the transaction
        # already rolled back; only roll back if it is still active.
        if transaction.is_active:
            transaction.rollback()
        connection.close()
