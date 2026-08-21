"""日次株価を ticks に書き込む.

同じ日を 2 回取り込んでも壊れない。(code, date) で upsert する。
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.collectors.yahoo import DailyQuote
from kabu_app.models import Stock, Tick

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1000

_UPDATABLE_COLUMNS = ("open", "high", "low", "close", "volume", "adjusted_close")
"""upsert で上書きする列.

adjusted_close を上書きするのが肝心。株式分割が起きると Yahoo は過去まで遡って書き換える
ので、取り直したときに古い値が残っていては意味がない。
"""


def latest_dates(session: Session) -> dict[str, date]:
    """銘柄ごとの最新取引日をまとめて引く. 差分取得の起点に使う."""
    rows = session.execute(select(Tick.code, func.max(Tick.date)).group_by(Tick.code))
    # Result そのものを dict() に渡すと keys() があるせいで Mapping 扱いになる。
    return dict(rows.tuples().all())


def listed_codes(session: Session) -> list[str]:
    """取得の対象になる銘柄コード.

    上場中の銘柄だけを返す。上場廃止すると Yahoo から株価のページごと消えるため、
    追いかけても取れない。廃止前の株価は取り込み済みのぶんが残る。
    """
    return list(
        session.execute(
            select(Stock.code).where(Stock.is_listed.is_(True)).order_by(Stock.code)
        ).scalars()
    )


def save_quotes(session: Session, quotes: Sequence[DailyQuote]) -> int:
    """株価を取り込む. コミットは呼び出し側の責任."""
    if not quotes:
        return 0

    unique = list({(quote.code, quote.date): quote for quote in quotes}.values())

    for chunk in _chunked([asdict(quote) for quote in unique]):
        statement = insert(Tick).values(list(chunk))
        statement = statement.on_conflict_do_update(
            index_elements=[Tick.code, Tick.date],
            set_={
                **{column: statement.excluded[column] for column in _UPDATABLE_COLUMNS},
                # onupdate は ORM の更新でしか効かないので明示する
                "updated_at": func.now(),
            },
        )
        session.execute(statement)

    session.flush()
    return len(unique)


def codes_with_price_jumps(session: Session, threshold: float = 0.55) -> list[str]:
    """終値が前日から大きく飛んでいる銘柄を返す.

    株式分割や併合の跡。adjusted_close を持たずに取り込んだ行を洗い出すために使う。
    実際の暴落と区別はつかないので、洗い直しの候補として見る。
    """
    previous = func.lag(Tick.close).over(partition_by=Tick.code, order_by=Tick.date)
    inner = select(Tick.code, Tick.close, previous.label("previous")).subquery()

    statement = (
        select(inner.c.code)
        .where(
            inner.c.previous.is_not(None),
            (inner.c.close < inner.c.previous * threshold)
            | (inner.c.close > inner.c.previous / threshold),
        )
        .distinct()
        .order_by(inner.c.code)
    )
    return list(session.execute(statement).scalars())


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]
