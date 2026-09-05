"""日次の株価."""

from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Date, Index, Numeric, String, Text
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


class TickJumpCheck(Base, TimestampMixin):
    """調整後終値の飛びのうち、取り直しても消えなかった箇所.

    ``kabu fetch ticks --only-jumps`` の判定は、分割の調整漏れと本物の急騰・急落を
    区別できない。低位株は 1 円刻みで動くので 3 円 → 1 円が起きる。売買が成立しない日が
    続いた銘柄は、値が付いた日に制限値幅を超えて飛ぶ。どちらも取り直しても値が変わらず、
    記録しないと毎晩同じ銘柄を取り直し続ける。

    飛びの位置だけを持ち、そのときの値は持たない。分割のたびに Yahoo が過去の調整後終値を
    書き換えるため、値で照合すると分割のたびに記録が無効になる。

    ``code`` に ticks への外部キーは張らない。上場廃止した銘柄の記録も残すため。
    """

    __tablename__ = "tick_jump_checks"
    __table_args__ = (
        {"comment": "取り直しても消えなかった調整後終値の飛び。次の判定から除外する"},
    )

    code: Mapped[str] = mapped_column(
        String(8), primary_key=True, comment="JPX 銘柄コード。ticks に合わせて外部キーは張らない"
    )
    date: Mapped[date] = mapped_column(
        Date, primary_key=True, comment="調整後終値が前日から飛んだ日"
    )
    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="除外した理由。自動記録では空。「低位株の 1 円刻み」などを後から手で書く",
    )
