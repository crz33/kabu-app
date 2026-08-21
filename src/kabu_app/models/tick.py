"""日次の株価."""

from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Date, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from kabu_app.models.base import Base, TimestampMixin

_PRICE = Numeric(12, 2)
"""株価。0.1 円単位の値が実在するので整数にはしない."""


class Tick(Base, TimestampMixin):
    """ある銘柄のある日の四本値と出来高.

    ``code`` に stocks への外部キーは張らない。上場廃止した銘柄の株価も残すため。
    市場指数 (TOPIX の 998405 など) も同じ表に入る。個別株と並べてβや相対リターンを
    計算するため。桁数で見分けられる。

    ``adjusted_close`` は株式分割を遡って調整した終値。Yahoo が分割のたびに過去まで
    書き換えるので、分割が起きた銘柄は全期間を取り直さないと古い値のまま残る。
    始値・高値・安値の調整値は提供されない。``adjusted_close / close`` を掛けて揃える。
    """

    __tablename__ = "ticks"
    __table_args__ = (
        Index("ix_ticks_date", "date"),
        {"comment": "日次の四本値と出来高 (Yahoo Finance)"},
    )

    code: Mapped[str] = mapped_column(
        String(8), primary_key=True, comment="JPX 銘柄コード。市場指数は 998405 のような 6 桁"
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True, comment="取引日")
    open: Mapped[Decimal] = mapped_column(_PRICE, nullable=False, comment="始値 (円)")
    high: Mapped[Decimal] = mapped_column(_PRICE, nullable=False, comment="高値 (円)")
    low: Mapped[Decimal] = mapped_column(_PRICE, nullable=False, comment="安値 (円)")
    close: Mapped[Decimal] = mapped_column(_PRICE, nullable=False, comment="終値 (円)")
    volume: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="出来高 (株)。6 億を超える日があるので 8 バイトで持つ"
    )
    adjusted_close: Mapped[Decimal | None] = mapped_column(
        _PRICE,
        nullable=True,
        comment="株式分割を調整した終値。findocgen から移した行は NULL",
    )
