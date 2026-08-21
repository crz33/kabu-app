"""DB を使うテストの共通フィクスチャ."""

from collections.abc import Iterator

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from kabu_app.config import get_settings
from kabu_app.db import create_db_engine
from kabu_app.models import EdinetDocument, Stock, StockSnapshot


@pytest.fixture
def session() -> Iterator[Session]:
    """空のテーブルを持つセッション。終了時にロールバックするので DB は元に戻る.

    DB に繋げないときはスキップする。削除は外部キーに参照される側があとになるよう
    並べてある。テーブルを足したらここにも足す。
    """
    engine = create_db_engine(get_settings().database_url)
    try:
        connection = engine.connect()
    except OperationalError as error:
        pytest.skip(f"DB に接続できないためスキップ: {error}")

    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    session.execute(delete(EdinetDocument))
    session.execute(delete(StockSnapshot))
    session.execute(delete(Stock))
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
