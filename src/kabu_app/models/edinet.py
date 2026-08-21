"""EDINET の提出書類メタデータ."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from kabu_app.models.base import Base, TimestampMixin


class EdinetDocument(Base, TimestampMixin):
    """EDINET 書類一覧 API から取った 1 件のメタデータ.

    今は有価証券報告書 (120) と訂正有価証券報告書 (130) だけを入れる。
    対象を広げるときは ``collectors.edinet.TARGET_DOC_TYPES`` に足す。
    DB 側に制約は置いていない。既存行を消さずに種別を増やせるようにするため。

    ``code`` に stocks への外部キーは張らない。上場廃止した会社の有報も残すため。
    上場中の銘柄に絞りたい分析は stocks と結合する。
    """

    __tablename__ = "edinet_documents"
    __table_args__ = (
        Index("ix_edinet_documents_submit_date", "submit_date"),
        Index("ix_edinet_documents_code_period_end", "code", "period_end"),
        {"comment": "EDINET 提出書類のメタデータ (有価証券報告書とその訂正)"},
    )

    doc_id: Mapped[str] = mapped_column(
        String(16), primary_key=True, comment="EDINET 書類管理番号 (例: S100YW7F)"
    )
    edinet_code: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="提出者の EDINET コード (例: E05729)。商号変更や上場廃止をまたいで変わらない",
    )
    sec_code: Mapped[str | None] = mapped_column(
        String(5), nullable=True, comment="EDINET の証券コード。末尾 0 埋めの 5 桁"
    )
    code: Mapped[str] = mapped_column(
        String(5),
        nullable=False,
        comment="JPX 銘柄コード。sec_code の末尾 0 を落としたもの。上場廃止した銘柄も入る",
    )
    doc_type_code: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="書類種別コード (120: 有報 / 130: 訂正有報)"
    )
    parent_doc_id: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="訂正報告書が訂正する対象の書類管理番号。有報自身は NULL",
    )
    submit_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="提出日。書類一覧 API を引いた日付と一致する"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True, comment="提出日時 (JST)。分までしか無い"
    )
    period_end: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="対象期間の末日 (決算日)。書類によっては入らない"
    )
    filer_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="提出者名")
    doc_description: Mapped[str] = mapped_column(String(500), nullable=False, comment="書類の説明")
    has_xbrl: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="XBRL 一式の ZIP を取得できるか (xbrlFlag)"
    )
    is_withdrawn: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="取り下げられた書類か (withdrawalStatus)。true なら本文を取りに行かない",
    )
    downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="ZIP を保存した日時。NULL なら未取得で、次の実行が拾い直す",
    )
