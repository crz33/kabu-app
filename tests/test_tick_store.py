"""株価の取り込みのテスト.

セッションは conftest のフィクスチャが用意する。テストの最後にロールバックされる。
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kabu_app.collectors.yahoo import DailyQuote
from kabu_app.models import Stock, Tick, TickJumpCheck
from kabu_app.stores.tick import (
    codes_missing_adjusted,
    codes_with_price_jumps,
    earliest_dates,
    latest_prices,
    listed_codes,
    price_jumps,
    save_jump_checks,
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

    assert len(latest_prices(session)) == 1


def test_取り直すと調整後終値が上がり直る(session: Session) -> None:
    """分割が起きると Yahoo は過去まで書き換える。古い値を残すと分割前の水準で残る."""
    save_quotes(session, [_quote()])
    save_quotes(session, [_quote(adjusted_close=Decimal("783"))])

    tick = session.get(Tick, ("7203", date(2026, 8, 21)))
    assert tick is not None
    session.refresh(tick)
    assert tick.adjusted_close == Decimal("783.00")


def test_銘柄ごとの最新取引日と調整後終値を返す(session: Session) -> None:
    """差分取得の起点に使う. 銘柄ごとに 1 回ずつ問い合わせない."""
    save_quotes(
        session,
        [
            _quote(date=date(2026, 8, 20)),
            _quote(date=date(2026, 8, 21), adjusted_close=Decimal("3132")),
            _quote(code="6758", date=date(2026, 8, 19)),
        ],
    )

    assert latest_prices(session) == {
        "7203": (date(2026, 8, 21), Decimal("3132.00")),
        "6758": (date(2026, 8, 19), Decimal("3132.00")),
    }


def test_調整後終値が無ければ終値で代用する(session: Session) -> None:
    """findocgen から移した行は adjusted_close が NULL のまま入っている."""
    save_quotes(session, [_quote(adjusted_close=Decimal("3132"))])
    session.execute(text("UPDATE ticks SET adjusted_close = NULL"))

    assert latest_prices(session)["7203"] == (date(2026, 8, 21), Decimal("3132.00"))


def test_銘柄ごとの最古の取引日を返す(session: Session) -> None:
    """分割を検出した銘柄を取り直す起点に使う."""
    save_quotes(
        session,
        [_quote(date=date(2026, 8, 20)), _quote(date=date(2026, 8, 21))],
    )

    assert earliest_dates(session) == {"7203": date(2026, 8, 20)}


def test_上場中の銘柄だけを対象にする(session: Session) -> None:
    """上場廃止すると Yahoo から株価のページごと消える."""
    session.add_all([_stock("7203"), _stock("9999", is_listed=False)])
    session.flush()

    assert listed_codes(session) == ["7203"]


def test_調整が行き届いていない銘柄を洗い出す(session: Session) -> None:
    """2961 は 6440 から 1555 に落ちていた。調整後を持たない行がこう見える."""
    save_quotes(
        session,
        [
            _quote(code="2961", date=date(2026, 7, 29), close=Decimal("6440")),
            _quote(code="2961", date=date(2026, 7, 30), close=Decimal("1555")),
            _quote(code="7203", date=date(2026, 7, 29), close=Decimal("2900")),
            _quote(code="7203", date=date(2026, 7, 30), close=Decimal("2950")),
        ],
    )
    session.execute(text("UPDATE ticks SET adjusted_close = NULL"))

    assert codes_with_price_jumps(session) == ["2961"]


def test_調整されていれば分割の日でも飛びとみなさない(session: Session) -> None:
    """close は分割の日に必ず飛ぶ。それは正常な値なので拾ってはいけない."""
    save_quotes(
        session,
        [
            _quote(
                code="2961",
                date=date(2026, 7, 29),
                close=Decimal("6440"),
                adjusted_close=Decimal("1610"),
            ),
            _quote(
                code="2961",
                date=date(2026, 7, 30),
                close=Decimal("1555"),
                adjusted_close=Decimal("1555"),
            ),
        ],
    )

    assert codes_with_price_jumps(session) == []


def test_併合で跳ね上がった銘柄も拾う(session: Session) -> None:
    """株式併合は逆向きに飛ぶ。どちらも調整が要る."""
    save_quotes(
        session,
        [
            _quote(code="1234", date=date(2026, 7, 29), close=Decimal("100")),
            _quote(code="1234", date=date(2026, 7, 30), close=Decimal("1000")),
        ],
    )
    session.execute(text("UPDATE ticks SET adjusted_close = NULL"))

    assert codes_with_price_jumps(session) == ["1234"]


def _low_priced_jump(session: Session) -> None:
    """5103 の 3 円 → 1 円。低位株は 1 円刻みなので取り直しても消えない."""
    save_quotes(
        session,
        [
            _quote(
                code="5103",
                date=date(2026, 8, 20),
                close=Decimal("3"),
                adjusted_close=Decimal("3"),
            ),
            _quote(
                code="5103",
                date=date(2026, 8, 21),
                close=Decimal("1"),
                adjusted_close=Decimal("1"),
            ),
        ],
    )


def test_飛びの箇所を銘柄と日で返す(session: Session) -> None:
    """記録に入れる単位。銘柄だけでは、同じ銘柄の別の飛びと区別できない."""
    _low_priced_jump(session)

    assert price_jumps(session) == [("5103", date(2026, 8, 21))]


def test_確認済みの飛びは対象から外れる(session: Session) -> None:
    """取り直しても消えない飛びを記録して、毎晩の取り直しを止める."""
    _low_priced_jump(session)
    assert codes_with_price_jumps(session) == ["5103"]

    save_jump_checks(session, [("5103", date(2026, 8, 21))])

    assert codes_with_price_jumps(session) == []


def test_確認済みでも同じ銘柄の別の日は拾う(session: Session) -> None:
    """記録は箇所ごと。分割の取りこぼしが後から起きても気づける."""
    _low_priced_jump(session)
    save_quotes(
        session,
        [
            _quote(
                code="5103",
                date=date(2026, 8, 24),
                close=Decimal("4"),
                adjusted_close=Decimal("4"),
            ),
        ],
    )
    save_jump_checks(session, [("5103", date(2026, 8, 21))])

    assert codes_with_price_jumps(session) == ["5103"]


def test_確認済みでも飛びの箇所としては返る(session: Session) -> None:
    """price_jumps は除外しない。記録の updated_at を伸ばすために毎回入れ直す."""
    _low_priced_jump(session)
    save_jump_checks(session, [("5103", date(2026, 8, 21))])

    assert price_jumps(session) == [("5103", date(2026, 8, 21))]


def test_同じ箇所を2回記録しても1行のまま(session: Session) -> None:
    save_jump_checks(session, [("5103", date(2026, 8, 21))])
    save_jump_checks(session, [("5103", date(2026, 8, 21))])

    assert session.scalar(select(func.count()).select_from(TickJumpCheck)) == 1


def test_銘柄を指定すると他の銘柄は見ない(session: Session) -> None:
    """--max-codes で後回しにした銘柄を記録しないため、取り直したぶんだけ渡す."""
    _low_priced_jump(session)
    save_quotes(
        session,
        [
            _quote(code="1234", date=date(2026, 8, 20), close=Decimal("100")),
            _quote(code="1234", date=date(2026, 8, 21), close=Decimal("1000")),
        ],
    )
    session.execute(text("UPDATE ticks SET adjusted_close = NULL WHERE code = '1234'"))

    assert price_jumps(session, codes=["5103"]) == [("5103", date(2026, 8, 21))]


def test_調整後終値が無い上場銘柄を返す(session: Session) -> None:
    """NULL の行が 1 つでもあれば対象。上場廃止と since より前の行は見ない."""
    session.add_all([_stock("7203"), _stock("6758"), _stock("9999", is_listed=False)])
    session.flush()
    save_quotes(
        session,
        [
            _quote(code="7203", date=date(2026, 8, 20), adjusted_close=None),
            _quote(code="7203", date=date(2026, 8, 21)),
            _quote(code="6758", date=date(2023, 12, 29), adjusted_close=None),
            _quote(code="6758", date=date(2026, 8, 21)),
            _quote(code="9999", date=date(2026, 8, 21), adjusted_close=None),
        ],
    )

    assert codes_missing_adjusted(session, since=date(2024, 1, 4)) == ["7203"]
