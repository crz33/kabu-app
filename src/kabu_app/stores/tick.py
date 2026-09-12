"""日次株価を ticks に書き込む.

同じ日を 2 回取り込んでも壊れない。(code, date) で upsert する。
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.collectors.yahoo import DailyQuote
from kabu_app.models import Stock, Tick, TickJumpCheck

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


def codes_missing_adjusted(session: Session, since: date) -> list[str]:
    """調整後終値が入っていない行を持つ上場中の銘柄.

    findocgen から移した行は adjusted_close が NULL のまま。分割の無い銘柄では close と
    同じ値なので、値としては欠けていない。それでも埋めるのは、分析のたびに「NULL は欠損か」
    「分割は直っているか」を確かめる手間と勘違いを無くすため。

    since 以降の行だけを見る。取り直しは since から今日までを取るので、それより前の行は
    埋まらず、対象に残り続けてしまう。
    """
    statement = (
        select(Tick.code)
        .join(Stock, Stock.code == Tick.code)
        .where(Stock.is_listed.is_(True), Tick.date >= since, Tick.adjusted_close.is_(None))
        .distinct()
        .order_by(Tick.code)
    )
    return list(session.execute(statement).scalars())


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
    """調整後終値が前日から大きく飛んでいる銘柄を返す. 確認済みの箇所は除く.

    見るのは close ではなく調整後の値。close は分割の日に必ず飛ぶが、それは正常な動き。
    調整後が飛んでいるときだけ「調整が行き届いていない」ことになる。

    adjusted_close が NULL の行は close で代用する。移した行と取り直した行を同じ式で
    判定できる。実際の暴落や併合とは区別がつかないので、洗い直しの候補として見る。

    tick_jump_checks に入っている箇所は外す。取り直しても消えなかった飛びで、本物の
    値動きにあたる。外さないと毎晩同じ銘柄を取り直し続ける。

    閾値に頼るので取りこぼす。1.1 分割のような小さい分割は拾えない。分割を追う本筋は
    差分取得の重なりで、こちらは保険として使う。

    期間では絞らない。全期間を走査しても、pk_ticks が (code, date) 順なのでソートが
    要らず 4 秒で終わる。直近だけ見る形にすると、分割の全期間取り直しが途中で失敗した
    銘柄を拾い損ねる。
    """
    checked = select(TickJumpCheck.code, TickJumpCheck.date)
    inner = _jump_statement(threshold).subquery()

    statement = (
        select(inner.c.code)
        .where(tuple_(inner.c.code, inner.c.date).not_in(checked))
        .distinct()
        .order_by(inner.c.code)
    )
    return list(session.execute(statement).scalars())


def price_jumps(
    session: Session,
    threshold: float = 0.55,
    codes: Sequence[str] | None = None,
) -> list[tuple[str, date]]:
    """飛んでいる箇所を (銘柄, 日) で返す. 確認済みかどうかは見ない.

    取り直したあとに残っている飛びを拾って ``save_jump_checks`` に渡すために使う。
    ここで確認済みを除くと、記録の更新日時が伸びなくなる。
    """
    return [
        (code, jumped_on) for code, jumped_on in session.execute(_jump_statement(threshold, codes))
    ]


def save_jump_checks(session: Session, jumps: Sequence[tuple[str, date]]) -> int:
    """確認済みの飛びを記録する. コミットは呼び出し側の責任.

    同じ箇所を何度入れても 1 行のまま。updated_at だけが伸びる。
    """
    if not jumps:
        return 0

    rows = [{"code": code, "date": jumped_on} for code, jumped_on in jumps]
    statement = insert(TickJumpCheck).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[TickJumpCheck.code, TickJumpCheck.date],
        set_={"updated_at": func.now()},
    )
    session.execute(statement)
    session.flush()
    return len(rows)


def _jump_statement(
    threshold: float, codes: Sequence[str] | None = None
) -> Select[tuple[str, date]]:
    """飛んでいる箇所を (銘柄, 日) で選ぶ文を組む.

    lag は銘柄ごとに区切るので、codes で絞っても判定は変わらない。
    """
    price = func.coalesce(Tick.adjusted_close, Tick.close)
    previous = func.lag(price).over(partition_by=Tick.code, order_by=Tick.date)
    source = select(Tick.code, Tick.date, price.label("price"), previous.label("previous"))
    if codes is not None:
        source = source.where(Tick.code.in_(codes))
    inner = source.subquery()

    return (
        select(inner.c.code, inner.c.date)
        .where(
            inner.c.previous.is_not(None),
            (inner.c.price < inner.c.previous * threshold)
            | (inner.c.price > inner.c.previous / threshold),
        )
        .order_by(inner.c.code, inner.c.date)
    )


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]
