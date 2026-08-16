"""銘柄一覧を stocks と stock_snapshots に書き込む.

同じ基準日で 2 回走っても壊れない。stocks は code、stock_snapshots は
(base_date, code) で upsert する。
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.collectors.jpx import JpxStockList
from kabu_app.models import Stock, StockSnapshot

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1000

_DATA_COLUMNS = (
    "name",
    "market_segment",
    "industry33_code",
    "industry33_name",
    "industry17_code",
    "industry17_name",
    "topix_scale_code",
    "topix_scale_name",
)


@dataclass(frozen=True, slots=True)
class LoadResult:
    """取り込み結果."""

    base_date: date
    total: int
    """スナップショットに書いた銘柄数."""

    added: int
    """stocks に無かった銘柄の数."""

    delisted: int
    """今回の一覧から消えて is_listed を false にした銘柄の数."""

    stocks_updated: bool
    """stocks を更新したか。より新しい基準日が既にある場合は false."""


def load_stock_list(session: Session, data: JpxStockList) -> LoadResult:
    """銘柄一覧を取り込む. コミットは呼び出し側の責任."""
    existing = {
        code: (base_date, is_listed)
        for code, base_date, is_listed in session.execute(
            select(Stock.code, Stock.base_date, Stock.is_listed)
        )
    }
    latest = max((base_date for base_date, _ in existing.values()), default=None)
    stale = latest is not None and data.base_date < latest

    incoming = {stock.code for stock in data.stocks}
    added = len(incoming - existing.keys())

    if stale:
        # 過去のファイルを後から流した場合。stocks は今の状態のほうが新しいので触らない。
        # ただし stock_snapshots の外部キーを満たすため、無い銘柄だけは足す。
        # 新しいファイルに載っていない銘柄なので上場廃止扱いでよい。
        logger.warning(
            "基準日 %s は既存の最新 %s より古い。stocks は更新せずスナップショットのみ書く",
            data.base_date,
            latest,
        )
        _insert_missing_stocks(session, data)
        delisted = 0
    else:
        _upsert_stocks(session, data)
        delisted = _mark_delisted(session, data.base_date, existing, incoming)

    _upsert_snapshots(session, data)
    session.flush()

    return LoadResult(
        base_date=data.base_date,
        total=len(data.stocks),
        added=added,
        delisted=delisted,
        stocks_updated=not stale,
    )


def _rows(data: JpxStockList) -> list[dict[str, Any]]:
    """JpxStock を列名どおりの dict にする. フィールド名は列名に揃えてある."""
    return [{**asdict(stock), "base_date": data.base_date} for stock in data.stocks]


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]


def _upsert_stocks(session: Session, data: JpxStockList) -> None:
    for chunk in _chunked(_rows(data)):
        statement = insert(Stock).values([{**row, "is_listed": True} for row in chunk])
        statement = statement.on_conflict_do_update(
            index_elements=[Stock.code],
            set_={
                **{column: statement.excluded[column] for column in _DATA_COLUMNS},
                "base_date": statement.excluded.base_date,
                "is_listed": statement.excluded.is_listed,
                # onupdate は ORM の更新でしか効かないので明示する
                "updated_at": func.now(),
            },
        )
        session.execute(statement)


def _insert_missing_stocks(session: Session, data: JpxStockList) -> None:
    for chunk in _chunked(_rows(data)):
        statement = insert(Stock).values([{**row, "is_listed": False} for row in chunk])
        session.execute(statement.on_conflict_do_nothing(index_elements=[Stock.code]))


def _mark_delisted(
    session: Session,
    base_date: date,
    existing: dict[str, tuple[date, bool]],
    incoming: set[str],
) -> int:
    """今回の一覧から消えた銘柄を上場廃止にする. 行は消さない."""
    delisted = [
        code for code, (_, is_listed) in existing.items() if is_listed and code not in incoming
    ]
    if not delisted:
        return 0

    session.execute(
        update(Stock)
        .where(Stock.code.in_(delisted))
        .values(is_listed=False, base_date=base_date, updated_at=func.now())
    )
    logger.info("上場廃止として is_listed を false にした: %s", ", ".join(sorted(delisted)))
    return len(delisted)


def _upsert_snapshots(session: Session, data: JpxStockList) -> None:
    for chunk in _chunked(_rows(data)):
        statement = insert(StockSnapshot).values(list(chunk))
        statement = statement.on_conflict_do_update(
            index_elements=[StockSnapshot.base_date, StockSnapshot.code],
            set_={column: statement.excluded[column] for column in _DATA_COLUMNS},
        )
        session.execute(statement)
