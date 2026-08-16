"""銘柄マスタと、その取得スナップショット."""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from kabu_app.models.base import Base, TimestampMixin

MARKET_SEGMENTS = ("prime", "standard", "growth")
"""取り込む市場区分。JPX の「プライム/スタンダード/グロース（内国株式）」に対応する."""

_MARKET_SEGMENT_CHECK = "market_segment IN ('prime', 'standard', 'growth')"


class Stock(Base, TimestampMixin):
    """銘柄マスタ。最新の状態だけを持つ.

    上場廃止した銘柄も行は消さず ``is_listed`` を false にする。
    過去の ``StockSnapshot`` から参照されるため。
    """

    __tablename__ = "stocks"
    __table_args__ = (
        CheckConstraint(_MARKET_SEGMENT_CHECK, name="market_segment"),
        Index("ix_stocks_market_segment", "market_segment"),
        Index("ix_stocks_industry33_code", "industry33_code"),
        {"comment": "銘柄マスタ (JPX 東証上場銘柄一覧の最新状態)"},
    )

    code: Mapped[str] = mapped_column(
        String(5),
        primary_key=True,
        comment="JPX 銘柄コード。4 桁英数字が基本だが優先株・種類株は 5 桁",
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="銘柄名")
    market_segment: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="市場区分 (prime / standard / growth)"
    )
    industry33_code: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="東証 33 業種コード (0 埋め 4 桁)"
    )
    industry33_name: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="東証 33 業種区分"
    )
    industry17_code: Mapped[str] = mapped_column(
        String(2), nullable=False, comment="東証 17 業種コード (0 埋め 2 桁)"
    )
    industry17_name: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="東証 17 業種区分"
    )
    topix_scale_code: Mapped[str | None] = mapped_column(
        String(1), nullable=True, comment="TOPIX 規模コード。対象外は NULL"
    )
    topix_scale_name: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="TOPIX 規模区分。対象外は NULL"
    )
    base_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="この行の元になった JPX 一覧の基準日"
    )
    is_listed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="上場中なら true",
    )


class StockSnapshot(Base):
    """基準日ごとの銘柄一覧。JPX ファイルの内容をそのまま積む.

    市場変更や業種変更を後から追うための履歴。取り込み時点では正規化以外の加工をしない。
    """

    __tablename__ = "stock_snapshots"
    __table_args__ = (
        CheckConstraint(_MARKET_SEGMENT_CHECK, name="market_segment"),
        Index("ix_stock_snapshots_code_base_date", "code", "base_date"),
        {"comment": "銘柄一覧の基準日ごとのスナップショット"},
    )

    base_date: Mapped[date] = mapped_column(
        Date, primary_key=True, comment="JPX 一覧の基準日 (ファイルの「日付」列)"
    )
    code: Mapped[str] = mapped_column(
        String(5),
        ForeignKey("stocks.code", ondelete="RESTRICT"),
        primary_key=True,
        comment="JPX 銘柄コード",
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="銘柄名")
    market_segment: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="市場区分 (prime / standard / growth)"
    )
    industry33_code: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="東証 33 業種コード (0 埋め 4 桁)"
    )
    industry33_name: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="東証 33 業種区分"
    )
    industry17_code: Mapped[str] = mapped_column(
        String(2), nullable=False, comment="東証 17 業種コード (0 埋め 2 桁)"
    )
    industry17_name: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="東証 17 業種区分"
    )
    topix_scale_code: Mapped[str | None] = mapped_column(
        String(1), nullable=True, comment="TOPIX 規模コード。対象外は NULL"
    )
    topix_scale_name: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="TOPIX 規模区分。対象外は NULL"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="行の作成日時",
    )
