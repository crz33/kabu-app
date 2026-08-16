"""銘柄一覧の取り込みのテスト.

実際の DB に繋いで動かす。書き込みはすべてトランザクション内で行い、
テストの最後に必ずロールバックする。DB に繋げないときはスキップする。
"""

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from kabu_app.collectors.jpx import JpxStock, JpxStockList
from kabu_app.config import get_settings
from kabu_app.db import create_db_engine
from kabu_app.models import Stock, StockSnapshot
from kabu_app.stores.stock import load_stock_list

JUNE = date(2026, 6, 30)
JULY = date(2026, 7, 31)
MAY = date(2026, 5, 31)


@pytest.fixture
def session() -> Iterator[Session]:
    """空のテーブルを持つセッション。終了時にロールバックするので DB は元に戻る."""
    engine = create_db_engine(get_settings().database_url)
    try:
        connection = engine.connect()
    except OperationalError as error:
        pytest.skip(f"DB に接続できないためスキップ: {error}")

    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    session.execute(delete(StockSnapshot))
    session.execute(delete(Stock))
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _stock(code: str) -> JpxStock:
    return JpxStock(
        code=code,
        name=f"銘柄{code}",
        market_segment="prime",
        industry33_code="0050",
        industry33_name="水産・農林業",
        industry17_code="01",
        industry17_name="食品",
        topix_scale_code=None,
        topix_scale_name=None,
    )


def _listing(base_date: date, *codes: str) -> JpxStockList:
    return JpxStockList(base_date=base_date, stocks=[_stock(code) for code in codes])


def _codes(session: Session, *, listed: bool) -> set[str]:
    return set(session.scalars(select(Stock.code).where(Stock.is_listed == listed)))


def test_同じ基準日を2回流しても増えない(session: Session) -> None:
    first = load_stock_list(session, _listing(JUNE, "1301", "1332"))
    second = load_stock_list(session, _listing(JUNE, "1301", "1332"))

    assert first.added == 2
    assert second.added == 0
    assert session.scalar(select(Stock.code).where(Stock.code == "1301")) == "1301"
    assert len(list(session.scalars(select(StockSnapshot.code)))) == 2


def test_一覧から消えた銘柄は上場廃止になる(session: Session) -> None:
    load_stock_list(session, _listing(JUNE, "1301", "1332"))
    result = load_stock_list(session, _listing(JULY, "1301"))

    assert result.delisted == 1
    assert _codes(session, listed=True) == {"1301"}
    assert _codes(session, listed=False) == {"1332"}


def test_上場廃止でも行は消さない(session: Session) -> None:
    """過去のスナップショットが外部キーで参照するため、行は残す."""
    load_stock_list(session, _listing(JUNE, "1301", "1332"))
    load_stock_list(session, _listing(JULY, "1301"))

    snapshots = set(session.execute(select(StockSnapshot.base_date, StockSnapshot.code)).all())
    assert snapshots == {(JUNE, "1301"), (JUNE, "1332"), (JULY, "1301")}


def test_古い基準日ではstocksを更新しない(session: Session) -> None:
    """過去のファイルを後から流しても、最新の状態を巻き戻さない."""
    load_stock_list(session, _listing(JULY, "1301"))
    result = load_stock_list(session, _listing(MAY, "1301", "1332"))

    assert result.stocks_updated is False
    assert session.scalar(select(Stock.base_date).where(Stock.code == "1301")) == JULY
    assert session.scalar(select(StockSnapshot.code).where(StockSnapshot.base_date == MAY))


def test_古い基準日にしかない銘柄は上場廃止として足す(session: Session) -> None:
    """外部キーを満たすために stocks へ入れる。新しい一覧に無いので上場中ではない."""
    load_stock_list(session, _listing(JULY, "1301"))
    load_stock_list(session, _listing(MAY, "1301", "1332"))

    assert _codes(session, listed=True) == {"1301"}
    assert _codes(session, listed=False) == {"1332"}
