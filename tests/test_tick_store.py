"""株価の取り込みのテスト.

セッションは conftest のフィクスチャが用意する。テストの最後にロールバックされる。
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from kabu_app.collectors.yahoo import DailyQuote
from kabu_app.models import Stock, Tick
from kabu_app.stores.tick import (
    codes_with_price_jumps,
    latest_dates,
    listed_codes,
    save_quotes,
)

_BASE = DailyQuote(
    code="7203",
    date=date(2026, 8, 21),
    open=Decimal("3066"),
    high=Decimal("3132"),
    low=Decimal("3056"),
    close=Decimal("3132"),
    volume=25924500,
    adjusted_close=Decimal("3132"),
)


def _quote(**overrides: object) -> DailyQuote:
    return replace(_BASE, **overrides)  # type: ignore[arg-type]


def _stock(code: str, is_listed: bool = True) -> Stock:
    return Stock(
        code=code,
        name=f"銘柄{code}",
        market_segment="prime",
        industry33_code="0050",
        industry33_name="水産・農林業",
        industry17_code="01",
        industry17_name="食品",
        topix_scale_code=None,
        topix_scale_name=None,
        base_date=date(2026, 7, 31),
        is_listed=is_listed,
    )


def test_株価を取り込む(session: Session) -> None:
    assert save_quotes(session, [_quote()]) == 1

    tick = session.get(Tick, ("7203", date(2026, 8, 21)))
    assert tick is not None
    assert tick.low == Decimal("3056.00")
    assert tick.volume == 25924500


def test_同じ日を2回入れても1行のまま(session: Session) -> None:
    save_quotes(session, [_quote(), _quote()])

    assert len(latest_dates(session)) == 1


def test_取り直すと調整後終値が上がり直る(session: Session) -> None:
    """分割が起きると Yahoo は過去まで書き換える。古い値を残すと分割前の水準で残る."""
    save_quotes(session, [_quote()])
    save_quotes(session, [_quote(adjusted_close=Decimal("783"))])

    tick = session.get(Tick, ("7203", date(2026, 8, 21)))
    assert tick is not None
    session.refresh(tick)
    assert tick.adjusted_close == Decimal("783.00")


def test_銘柄ごとの最新取引日を返す(session: Session) -> None:
    """差分取得の起点に使う. 銘柄ごとに 1 回ずつ問い合わせない."""
    save_quotes(
        session,
        [
            _quote(date=date(2026, 8, 20)),
            _quote(date=date(2026, 8, 21)),
            _quote(code="6758", date=date(2026, 8, 19)),
        ],
    )

    assert latest_dates(session) == {"7203": date(2026, 8, 21), "6758": date(2026, 8, 19)}


def test_上場中の銘柄だけを対象にする(session: Session) -> None:
    """上場廃止すると Yahoo から株価のページごと消える."""
    session.add_all([_stock("7203"), _stock("9999", is_listed=False)])
    session.flush()

    assert listed_codes(session) == ["7203"]


def test_終値が飛んでいる銘柄を洗い出す(session: Session) -> None:
    """株式分割の跡。2961 は 6440 から 1555 に落ちていた."""
    save_quotes(
        session,
        [
            _quote(code="2961", date=date(2026, 7, 29), close=Decimal("6440")),
            _quote(code="2961", date=date(2026, 7, 30), close=Decimal("1555")),
            _quote(code="7203", date=date(2026, 7, 29), close=Decimal("2900")),
            _quote(code="7203", date=date(2026, 7, 30), close=Decimal("2950")),
        ],
    )

    assert codes_with_price_jumps(session) == ["2961"]


def test_併合で跳ね上がった銘柄も拾う(session: Session) -> None:
    """株式併合は逆向きに飛ぶ。どちらも調整が要る."""
    save_quotes(
        session,
        [
            _quote(code="1234", date=date(2026, 7, 29), close=Decimal("100")),
            _quote(code="1234", date=date(2026, 7, 30), close=Decimal("1000")),
        ],
    )

    assert codes_with_price_jumps(session) == ["1234"]
