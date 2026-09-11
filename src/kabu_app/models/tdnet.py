"""TDnet の適時開示のメタデータ."""

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
)
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
        Index("ix_tdnet_disclosures_code_fiscal_year_end", "code", "fiscal_year_end"),
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
    accounting_standard: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="会計基準 (Japan GAAP / IFRS / US GAAP)。解析するまで NULL",
    )
    is_consolidated: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="連結の短信か。解析するまで NULL",
    )
    fiscal_year_end: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="表紙から読んだ会計年度末。どの期の短信かを表す。解析するまで NULL",
    )
    quarter: Mapped[str | None] = mapped_column(
        String(2),
        nullable=True,
        comment="四半期の区分 (Q1 / Q2 / Q3 / FY)。解析するまで NULL",
    )
    parsed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="iXBRL を解析した日時。NULL なら未解析で、次の実行が拾い直す",
    )
    parse_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="解析に失敗した理由。次の実行で解析し直すと消える。成功した書類は NULL",
    )


class TdnetSummaryFact(Base, TimestampMixin):
    """決算短信の表紙から取り出した数値 1 つ.

    表紙は ``tse-ed-t`` の独自体系で、有報とも添付の財務諸表とも要素名が重ならない。
    ここを読むのは**会社予想**のためになる。予想は有報に無く、決算短信でしか取れない。

    実績もここに載るが、値は ``scale="6"`` で百万円に丸めてある。同じ売上高が表紙で
    138,877 (百万円)、添付で 138,877,139 (円) になる。実績は ``tdnet_statement_facts``
    を使うこと。

    ``context_ref`` を軸に割って列に持つ。有報の ``member`` と違い、短信の context は
    意味の決まった軸の掛け合わせになっている。文字列のまま置くと、予想を引くたびに
    ``LIKE '%ForecastMember'`` を書くことになる。
    """

    __tablename__ = "tdnet_summary_facts"
    __table_args__ = (
        Index("ix_tdnet_summary_facts_concept_period_end", "concept", "period_end"),
        Index("ix_tdnet_summary_facts_fact_type", "fact_type"),
        {"comment": "決算短信の表紙の数値 (実績と会社予想)"},
    )

    doc_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("tdnet_disclosures.doc_id", ondelete="CASCADE"),
        primary_key=True,
        comment="TDnet 書類 ID (FK: tdnet_disclosures.doc_id)",
    )
    concept: Mapped[str] = mapped_column(
        String(256),
        primary_key=True,
        comment="要素名 (例: tse-ed-t_NetSales)。表紙は tse-ed-t だけで会社独自の拡張は無い",
    )
    context_ref: Mapped[str] = mapped_column(
        String(256), primary_key=True, comment="原文の context の id。軸に割る前のもの"
    )
    scope: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="Current (当期) / Prior (前期) / Next (来期)"
    )
    period_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Year (通期) / AccumulatedQ1〜Q3 (四半期累計) など",
    )
    quarter_member: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="配当の内訳に付く FirstQuarter / YearEnd / Annual など。無ければ NULL",
    )
    is_consolidated: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="連結の値か。context に区分が無ければ NULL"
    )
    fact_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Result (実績) / Forecast (会社予想) / Upper・Lower (予想レンジの上下限)",
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
        Numeric,
        nullable=False,
        comment="値。scale を掛けたあとの数。金額は円、比率は小数のまま",
    )
    unit: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="単位 (JPY / Pure / JPYPerShares / Shares)"
    )
    decimals: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="原文の精度表示。値のスケールとは関係しない"
    )


class TdnetStatementFact(Base, TimestampMixin):
    """決算短信の添付にある財務諸表の数値 1 つ.

    添付は ``jppfs_cor`` / ``jpigp_cor`` と**有報とまったく同じ体系**になる。だから
    ``edinet_financials`` の名寄せがそのまま効く。有報が年 1 回なのに対し、こちらは
    四半期ごとに入るので粒度が上がる。

    値は円のまま入る。表紙と違って丸めが無い。

    ``ordinal`` は iXBRL に出てくる順の通し番号。計算書ごとにファイルが分かれており、
    出現順が刷られた並びと一致する。有報と違って ``depth`` は持たない。階層は同梱の
    ``-pre.xml`` にあるので、要るようになったらそこから足せる。
    """

    __tablename__ = "tdnet_statement_facts"
    __table_args__ = (
        Index("ix_tdnet_statement_facts_concept_period_end", "concept", "period_end"),
        Index("ix_tdnet_statement_facts_period_end", "period_end"),
        {"comment": "決算短信の添付にある財務諸表の数値 (1 行 1 数値)"},
    )

    doc_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("tdnet_disclosures.doc_id", ondelete="CASCADE"),
        primary_key=True,
        comment="TDnet 書類 ID (FK: tdnet_disclosures.doc_id)",
    )
    ordinal: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        comment="書類を通した通し番号。計算書に刷られる順になる",
    )
    section: Mapped[str] = mapped_column(
        String(4),
        nullable=False,
        comment="計算書。BS / PL / PC / CI / CF / SS / SG。IFRS の財政状態計算書は BS に寄せる",
    )
    concept: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="要素名 (例: jppfs_cor_NetSales)。有報と同じ体系で、会社独自の拡張も入る",
    )
    context_ref: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="XBRL の context の id。期間と区分を指す"
    )
    term: Mapped[str] = mapped_column(
        String(1),
        nullable=False,
        comment="どの期の短信か。a (通期) / q (四半期) / s (中間)",
    )
    is_consolidated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="連結の計算書か。ファイル名の 2 文字目で決まる"
    )
    member: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="context_ref から期間の部分を除いた残り。全体の値は NULL",
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
        Numeric, nullable=False, comment="値。円のまま入れる。丸めるのは表示側の仕事"
    )
    unit: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="単位 (JPY / Pure / JPYPerShares / Shares)"
    )
    decimals: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="原文の精度表示。値のスケールとは関係しない"
    )


class TdnetFinancial(Base, TimestampMixin):
    """決算短信の添付を名寄せした財務項目 1 つ.

    有報の ``edinet_financials`` と同じ項目に寄せてある。添付は ``jppfs_cor`` /
    ``jpigp_cor`` と有報と同じ体系なので、名寄せの定義を共有できる。有報が年 1 回なのに
    対し、こちらは四半期ごとに入る。

    有報と違うのは ``period_kind`` を持つこと。短信は同じ期末に**年初来累計と単独四半期**が
    並ぶ。「Q2 累計 100 億」と「Q2 単独 50 億」を取り違えると致命的なので、主キーに含める。

    連結と単体はどちらか一方だけを入れる。連結財務諸表を作る会社は連結、作らない会社は
    単体になる。有報と同じ扱いで、``tdnet_disclosures.is_consolidated`` で選ぶ。

    実績はこちらを使うこと。表紙 (``tdnet_summary_facts``) にも同じ数値が載るが、
    百万円に丸めてある。表紙を読むのは会社予想のためになる。

    添付が空の書類だけは表紙から寄せる。``source_section`` が ``SM`` の行がそれで、値は
    百万円に丸まっている。米国基準の会社は決算短信の添付 XBRL を出さず、数値データの訂正
    短信も表紙だけを出し直すため。表紙は累計しか載せないので、単独四半期 (``quarter``) の
    行は入らない。中間期も ``interim`` ではなく ``ytd`` になる。表紙の期の呼び方に中間を
    表すものが無く、``AccumulatedQ2`` としか書かれていないため。
    """

    __tablename__ = "tdnet_financials"
    __table_args__ = (
        Index("ix_tdnet_financials_item_period_end", "item", "period_end"),
        Index("ix_tdnet_financials_period_kind", "period_kind"),
        {"comment": "決算短信の添付を共通の財務項目に名寄せした値"},
    )

    doc_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("tdnet_disclosures.doc_id", ondelete="CASCADE"),
        primary_key=True,
        comment="TDnet 書類 ID (FK: tdnet_disclosures.doc_id)",
    )
    item: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="財務項目 (net_sales/operating_income/ordinary_income/net_income/"
        "total_assets/net_assets)。有報の edinet_financials と同じ",
    )
    period_kind: Mapped[str] = mapped_column(
        String(16),
        primary_key=True,
        comment="期の種類。ytd (年初来累計) / quarter (単独四半期) / year (通期) / "
        "interim (中間) と、時点の quarter_end / year_end / interim_end",
    )
    period_end: Mapped[date] = mapped_column(
        Date, primary_key=True, comment="期間の末日、または時点の日付"
    )
    period_start: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="期間の開始日。時点の項目では NULL"
    )
    value: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, comment="値。円のまま入れる。丸めるのは表示側の仕事"
    )
    unit: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="単位 (JPY)。元のファクトのものをそのまま持つ"
    )
    source_section: Mapped[str] = mapped_column(
        String(4),
        nullable=False,
        comment="どこから取ったか。PL: 損益計算書 / BS: 貸借対照表",
    )
    source_concept: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="元の XBRL 要素名。名寄せの判断を後から検証するために必ず残す",
    )
