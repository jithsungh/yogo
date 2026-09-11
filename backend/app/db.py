"""
SQLAlchemy engine + session factory.

Import get_session() everywhere — it yields a managed Session that
auto-rolls-back on exception and closes on exit.
"""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()
_engine = create_engine(_settings.database_url)
SessionLocal = sessionmaker(bind=_engine)

from sqlalchemy import event
from pgvector.psycopg import register_vector

@event.listens_for(_engine, "connect")
def connect(dbapi_connection, connection_record):
    register_vector(dbapi_connection)


@contextmanager
def get_session() -> Session:
    """Yield a transactional session. Rolls back on exception, closes on exit."""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
