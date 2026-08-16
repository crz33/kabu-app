"""データベース接続."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def create_db_engine(database_url: str) -> Engine:
    """Engine を作る."""
    return create_engine(database_url, future=True)


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    """セッションファクトリを作る."""
    return sessionmaker(bind=create_db_engine(database_url), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """トランザクション境界。例外が出たらロールバックする."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
