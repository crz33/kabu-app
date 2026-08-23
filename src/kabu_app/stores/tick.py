"""日次株価を ticks に書き込む.

同じ日を 2 回取り込んでも壊れない。(code, date) で upsert する。
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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


def latest_prices(session: Session) -> dict[str, tuple[date, Decimal]]:
    """銘柄ごとの最新取引日と、その日の調整後終値をまとめて引く.

    差分取得はこの日から取り直して 1 日重ねる。重ねた日の調整後終値が変わっていれば、
    株式分割で過去まで書き換わったと分かる。分割は過去の行を書き換えるので、翌日から
    取っていては永久に気づけない。

    adjusted_close が NULL の行は close で代用する。findocgen から移した行がこれに当たる。
    """
    price = func.coalesce(Tick.adjusted_close, Tick.close)
    statement = (
        select(Tick.code, Tick.date, price)
        .distinct(Tick.code)
        .order_by(Tick.code, Tick.date.desc())
    )
    return {code: (traded_on, value) for code, traded_on, value in session.execute(statement)}


def earliest_dates(session: Session) -> dict[str, date]:
    """銘柄ごとの最古の取引日. 分割を検出した銘柄を取り直す起点に使う."""
    rows = session.execute(select(Tick.code, func.min(Tick.date)).group_by(Tick.code))
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


RETRY_INTERVAL = timedelta(days=180)
"""飛びのある銘柄を取り直してから、次に候補へ戻すまでの間隔.

閾値に引っかかる値動きが実際に起きる。低位株なら 1 日で 1.8 倍は普通で、取り直しても飛びは
消えない。そういう銘柄がコード順の先頭に居座ると、毎晩 50 件の枠を食い続ける。

``adjusted_close`` が入っているかどうかでは判別できない。日次の差分取得でも入るためで、
「取り直した」ことの証拠にならない。2025 年 1 月に差分で入った行に、その後 6 月の分割が
反映されないまま残る、という漏れ方をする。

そこで時間で見る。半年おけば分割はたいてい 1 回あるかないかで、見落としても次の機会に拾える。
時間に頼るからこそ、判定を誤っても自己修復する。
"""


def codes_with_price_jumps(
    session: Session,
    threshold: float = 0.55,
    retry_interval: timedelta = RETRY_INTERVAL,
) -> list[str]:
    """調整後終値が前日から大きく飛んでいる銘柄を返す.

    見るのは close ではなく調整後の値。close は分割の日に必ず飛ぶが、それは正常な動き。
    調整後が飛んでいるときだけ「調整が行き届いていない」ことになる。

    adjusted_close が NULL の行は close で代用する。移した行と取り直した行を同じ式で
    判定できる。実際の暴落や併合とは区別がつかないので、洗い直しの候補として見る。

    最近取り直した銘柄は外す。``retry_interval`` のあいだ空ける。飛びのあった日の行の
    ``updated_at`` で見るので、進捗を別に持たなくてよい。日次の差分取得は過去の行を触らない
    ため、取り直したときだけこの日付が動く。
    """
    price = func.coalesce(Tick.adjusted_close, Tick.close)
    previous = func.lag(price).over(partition_by=Tick.code, order_by=Tick.date)
    inner = select(
        Tick.code,
        price.label("price"),
        previous.label("previous"),
        Tick.updated_at.label("updated_at"),
    ).subquery()

    statement = (
        select(inner.c.code)
        .where(
            inner.c.previous.is_not(None),
            (inner.c.price < inner.c.previous * threshold)
            | (inner.c.price > inner.c.previous / threshold),
            inner.c.updated_at < datetime.now(UTC) - retry_interval,
        )
        .distinct()
        .order_by(inner.c.code)
    )
    return list(session.execute(statement).scalars())


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]
