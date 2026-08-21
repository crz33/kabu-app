"""TDnet の適時開示のメタデータ."""

from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, Index, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from kabu_app.models.base import Base, TimestampMixin


class TdnetDisclosure(Base, TimestampMixin):
    """TDnet の適時開示 1 件のメタデータ.

    今は決算短信だけを入れる。訂正短信も入れる。対象を広げるときは
    ``collectors.tdnet.is_target_disclosure`` を変える。

    ``code`` に stocks への外部キーは張らない。上場廃止した会社の開示も残すため。
    上場中の銘柄に絞りたい分析は stocks と結合する。

    一覧も実体ファイルも 31 日ほどで消える。取り逃すと二度と取れない。
    """

    __tablename__ = "tdnet_disclosures"
    __table_args__ = (
        Index("ix_tdnet_disclosures_disclosed_date", "disclosed_date"),
        Index("ix_tdnet_disclosures_code_disclosed_date", "code", "disclosed_date"),
        {"comment": "TDnet の適時開示のメタデータ (決算短信とその訂正)"},
    )

    doc_id: Mapped[str] = mapped_column(
        String(24),
        primary_key=True,
        comment="TDnet 書類 ID。PDF のファイル名から拡張子を除いたもの",
    )
    disclosed_date: Mapped[date] = mapped_column(Date, nullable=False, comment="開示日")
    disclosed_time: Mapped[time] = mapped_column(
        Time, nullable=False, comment="開示時刻。一覧には分までしか出ない"
    )
    sec_code: Mapped[str | None] = mapped_column(
        String(5),
        nullable=True,
        comment="TDnet の証券コード。末尾 0 埋めの 5 桁。findocgen から移した行は NULL",
    )
    code: Mapped[str] = mapped_column(
        String(5),
        nullable=False,
        comment="JPX 銘柄コード。sec_code の末尾 0 を落としたもの。上場廃止した銘柄も入る",
    )
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="開示した会社名")
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="開示の表題")
    markets: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="上場市場の略号 (例: 東札福)。東証以外の単独上場もある。移した行は NULL",
    )
    is_amendment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="訂正の開示か。表題に「訂正」が入るかで判定する"
    )
    xbrl_file: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="XBRL の ZIP のファイル名。NULL なら XBRL が無く、PDF だけが本体になる",
    )
    downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="実体を保存した日時。NULL なら未取得。31 日を過ぎると取れなくなる",
    )
