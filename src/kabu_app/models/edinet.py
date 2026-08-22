"""EDINET の提出書類メタデータと、そこから取り出した財務数値."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
)
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
    parsed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="XBRL を解析した日時。NULL なら未解析で、次の実行が拾い直す",
    )
    parse_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="解析に失敗した理由。次の実行で解析し直すと消える。成功した書類は NULL",
    )


class EdinetFact(Base, TimestampMixin):
    """有報の XBRL から取り出した財務諸表の数値 1 つ.

    ``context_ref`` を主キーに含めるのが要点になる。同じ勘定・同じ期間でも、連結全体の
    値とセグメント別の値が別の行として並ぶ。連結全体だけが欲しいときは ``member IS NULL``
    で絞る。これを潰して 1 行にまとめると、後から区別できなくなる。

    値は円のまま入れる。有報の表は百万円単位で刷られるが、XBRL の中身は円で、桁を丸める
    のは表示側の仕事になる。``decimals`` は原文の精度表示をそのまま残したもので、値の
    スケールとは関係しない。
    """

    __tablename__ = "edinet_facts"
    __table_args__ = (
        Index("ix_edinet_facts_concept_period_end", "concept", "period_end"),
        Index("ix_edinet_facts_period_end", "period_end"),
        {"comment": "有価証券報告書の XBRL から取り出した財務諸表の数値 (1 行 1 数値)"},
    )

    doc_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("edinet_documents.doc_id", ondelete="CASCADE"),
        primary_key=True,
        comment="EDINET 書類管理番号 (FK: edinet_documents.doc_id)",
    )
    section: Mapped[str] = mapped_column(
        String(8),
        primary_key=True,
        comment="どの計算書か。BR: 経営指標の推移 / BR_C: 同 提出会社 / BS / PL / CS",
    )
    concept: Mapped[str] = mapped_column(
        String(512),
        primary_key=True,
        comment="XBRL の要素名 (例: jppfs_cor_OperatingIncome)。会社独自の拡張も入る",
    )
    context_ref: Mapped[str] = mapped_column(
        String(512),
        primary_key=True,
        comment="XBRL の context の id。期間と区分を指す (例: CurrentYearDuration)",
    )
    member: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="context_ref から期間の部分を除いた残り。連結全体の値は NULL",
    )
    ordinal: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        comment="計算書に刷られる順。表示リンクを深さ優先でたどった通し番号",
    )
    depth: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        comment="階層の深さ。0 が計算書そのもので、勘定は 1 以上",
    )
    period_type: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="duration (期間) か instant (時点) か"
    )
    period_start: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="期間の開始日。instant では NULL"
    )
    period_end: Mapped[date] = mapped_column(
        Date, nullable=False, comment="期間の末日、または時点の日付"
    )
    value: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, comment="値。金額は円、株数は株、比率は小数のまま"
    )
    unit: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="単位 (JPY / shares / pure / JPYPerShares)"
    )
    decimals: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="原文の精度表示 (-6 なら百万円の位まで有効)"
    )


class EdinetLabel(Base, TimestampMixin):
    """金融庁のタクソノミが定める、要素名の標準ラベル.

    ``edinet_facts`` の各行に文言を持たせると、同じ文字列が何百万回も重複する。表示のとき
    だけ結合する。

    年度は列に持たない。文言が変わったら後から読んだ方で上書きする。2024 から 2026 の 3 年で
    変わったのは 14 要素だけで、多くは送り仮名や法令の条番号だった。ただし 4 件は勘定科目名で、
    新リース会計基準に伴う「リース債務」から「リース負債」への言い換えが入る。指す対象は
    同じで要素名も変わらないため、集計には効かない。過去の有報を原本どおりの文言で表示したく
    なったら、主キーに年度を足して書類の年度で引く形に変えること。

    年度を持たない代わりに、要素は消さずに溜める。複数年を流すと和集合になり、廃止された
    要素も残る。2022 年の有報でも 2024 年以降の 3 年分で勘定の 98% が引けた。

    会社が付けた言い換えはここに入れない。``edinet_document_labels`` に分ける。
    """

    __tablename__ = "edinet_labels"
    __table_args__ = ({"comment": "金融庁のタクソノミが定める要素名の標準ラベル"},)

    concept: Mapped[str] = mapped_column(
        String(512), primary_key=True, comment="XBRL の要素名 (例: jppfs_cor_OperatingIncome)"
    )
    label: Mapped[str] = mapped_column(String(500), nullable=False, comment="日本語の標準ラベル")


class EdinetDocumentLabel(Base, TimestampMixin):
    """書類に同梱されていた、その書類でのラベル.

    書類ごとに持つ。会社は標準の勘定に独自の言い換えを付ける。実測では書類が独自ラベルを
    付けた標準要素 141 個のうち 20 個で、会社ごとに文言が割れていた。``jppfs_cor_OperatingIncome``
    に「セグメント利益」と書く会社がある。全社で 1 行にまとめると、他社の営業利益にその
    文言が出てしまう。

    会社独自の拡張要素のラベルもここにしか無い。要素名に EDINET コードが入るため、こちらは
    書類をまたいでも衝突しない。
    """

    __tablename__ = "edinet_document_labels"
    __table_args__ = ({"comment": "有報に同梱されていた、その書類での要素名のラベル"},)

    doc_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("edinet_documents.doc_id", ondelete="CASCADE"),
        primary_key=True,
        comment="EDINET 書類管理番号 (FK: edinet_documents.doc_id)",
    )
    concept: Mapped[str] = mapped_column(String(512), primary_key=True, comment="XBRL の要素名")
    label: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="この書類でのラベル。標準ラベルより優先する"
    )


class EdinetShareholder(Base, TimestampMixin):
    """有報「大株主の状況」の 1 行.

    有報は上位 10 名までしか載せない。``ratio`` を全部足しても 100% にはならない。

    ``kind`` と ``is_owner`` は株主名から機械的に判定したもので、外れる例が残る。判定を
    後から見直せるよう ``name`` を必ず一緒に持つ。
    """

    __tablename__ = "edinet_shareholders"
    __table_args__ = (
        Index("ix_edinet_shareholders_code_period_end", "code", "period_end"),
        {"comment": "有価証券報告書「大株主の状況」の上位株主とオーナー判定"},
    )

    doc_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("edinet_documents.doc_id", ondelete="CASCADE"),
        primary_key=True,
        comment="EDINET 書類管理番号 (FK: edinet_documents.doc_id)",
    )
    rank: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, comment="大株主の順位。1 が筆頭"
    )
    code: Mapped[str] = mapped_column(String(5), nullable=False, comment="JPX 銘柄コード")
    period_end: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="対象期間の末日。書類によっては入らない"
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="株主名。原文のまま")
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="所有株式数 (株)")
    ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 2),
        nullable=True,
        comment="発行済株式総数に対する保有割合 (%)。XBRL の小数を 100 倍したもの",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="分類 (individual/director/asset_mgmt/employee/trust/public/corporate)",
    )
    is_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="オーナー系株主か。owner_ratio を出すときの合算対象"
    )
