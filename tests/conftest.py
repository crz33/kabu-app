"""DB を使うテストの共通フィクスチャ."""

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from kabu_app.config import get_settings
from kabu_app.db import create_db_engine

_TABLES = (
    "stocks, stock_snapshots, edinet_documents, edinet_facts, edinet_labels, "
    "edinet_document_labels, edinet_shareholders, edinet_financials, tdnet_disclosures, "
    "tdnet_summary_facts, tdnet_statement_facts, tdnet_financials, ticks"
)
"""空にするテーブル。テーブルを足したらここにも足す.

DELETE ではなく TRUNCATE を使う。ticks は 200 万行を超えるので、テストごとに全行を
消して戻すと 1 件あたり数秒かかる。TRUNCATE ならトランザクション内で巻き戻せて速い。
"""


@pytest.fixture(scope="session")
def engine() -> Engine:
    """テスト全体で 1 つの Engine を使い回す.

    Engine は接続プールを持つ。テストごとに作ると、テストの数だけプールが積み上がって
    接続を食い潰す。ラズパイの ``max_connections`` は 20 しかなく、実際にこれで
    「remaining connection slots are reserved」に当たった。
    """
    return create_db_engine(get_settings().database_url)


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """空のテーブルを持つセッション。終了時にロールバックするので DB は元に戻る.

    DB に繋げないときはスキップする。
    """
    try:
        connection = engine.connect()
    except OperationalError as error:
        pytest.skip(f"DB に接続できないためスキップ: {error}")

    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    session.execute(text(f"TRUNCATE TABLE {_TABLES} CASCADE"))
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
