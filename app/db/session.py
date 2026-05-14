"""
Database session management.

Provides the SQLAlchemy engine and a FastAPI dependency that yields a
short-lived :class:`~sqlmodel.Session` per request.

We use *synchronous* SQLAlchemy/SQLModel deliberately: the Phase 1 workload is
a single synthetic patient and a handful of documents, so async would add
complexity (async migrations, async test fixtures) for no throughput benefit.
The session layer is isolated in this module, so a later move to async would
touch only this file.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlmodel import Session

from app.core.config import get_settings

# echo=False keeps logs readable; flip to True locally to see emitted SQL.
# pool_pre_ping guards against stale connections after the DB restarts.
engine = create_engine(
    get_settings().database_url,
    echo=False,
    pool_pre_ping=True,
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yield a database session, closed after the request."""
    with Session(engine) as session:
        yield session
