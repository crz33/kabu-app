"""日次の株価."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Index, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from kabu_app.models.base import Base, TimestampMixin

_PRICE = Numeric(12, 2)
"""株価。0.1 円単位の値が実在するので整数にはしない."""


class Tick(Base, TimestampMixin):
    """ある銘柄のある日の四本値と出来高.

    ``code`` に stocks への外部キーは張らない。上場廃止した銘柄の株価も残すため。

    市場指数は入れない。TOPIX (998405) の 638 行が findocgen から移って残っていたが、
    2026-08-14 で止まったまま増えないので消した。Yahoo は指数の株価時系列ページを返さない。
    ``.T`` も ``.O`` も HTTP 200 で戻るものの ``histories`` が空、``pager`` が null になる。
    日経平均 (998407) も同じ。

    ベンチマークが要るようになったら、JPX や日経が公開する指数データを別の経路で取ること。

    ``adjusted_close`` は株式分割を遡って調整した終値。Yahoo が分割のたびに過去まで
    書き換えるので、分割が起きた銘柄は全期間を取り直さないと古い値のまま残る。
    始値・高値・安値の調整値は提供されない。``adjusted_close / close`` を掛けて揃える。

    これを持たないとバックテストが壊れる。前身の findocgen は捨てていて、234 万行のうち
    590 か所・561 銘柄に「前日比 45% 以上の飛び」が残っていた。全部が分割で、実際には
    起きていない暴落になる。分割するのは株価が上がった会社が多いので、捨てると成績が
    体系的に甘くなる。
    """

    __tablename__ = "ticks"
    __table_args__ = (
        Index("ix_ticks_date", "date"),
        {"comment": "日次の四本値と出来高 (Yahoo Finance)"},
    )

    code: Mapped[str] = mapped_column(
        String(8),
        primary_key=True,
        comment="JPX 銘柄コード。4 桁英数字が基本。優先株・種類株は 5 桁。市場指数は入れない",
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
    # TimestampMixin の汎用文を上書きする。この表では日時そのものが判定の記録にあたる
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="行の作成日時。この飛びを最初に確認した日時にあたる",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="行の更新日時。同じ飛びを再確認するたびに更新される",
    )
