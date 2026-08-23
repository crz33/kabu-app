"""決算短信の iXBRL を解析して数値を置く場所を作る.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-23

決算短信の ZIP には体系のまったく違う 2 系統が入っている。どちらも読むが、役割が重ならない
のでテーブルを分ける。

``tdnet_summary_facts`` は表紙 (``XBRLData/Summary/``)。``tse-ed-t`` の独自体系で、
**会社予想**が載る。予想は有報に無く、決算短信でしか取れない。実績も載るが ``scale="6"``
で百万円に丸めてあり、同じ売上高が表紙で 138,877、添付で 138,877,139 になる。

``tdnet_statement_facts`` は添付 (``XBRLData/Attachment/``)。``jppfs_cor`` / ``jpigp_cor``
と有報とまったく同じ体系で、内訳と円単位の精度がある。``edinet_financials`` の名寄せが
そのまま効き、年 1 回だった粒度が四半期になる。実績はこちらを使う。

書類の素性は表紙の ``DocumentName`` から読む。有報の DEI にあたるものが短信には無い。
表題が「第１四半期決算短信〔日本基準〕（連結）」の形になっており、実測 260 書類すべてに
入っていた。

適用したあとに `kabu parse tdnet` を流すと埋まる。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMPS = (
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
        comment="行の作成日時",
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
        comment="行の更新日時",
    ),
)


def upgrade() -> None:
    op.add_column(
        "tdnet_disclosures",
        sa.Column(
            "accounting_standard",
            sa.String(16),
            nullable=True,
            comment="会計基準 (Japan GAAP / IFRS / US GAAP)。解析するまで NULL",
        ),
    )
    op.add_column(
        "tdnet_disclosures",
        sa.Column(
            "is_consolidated", sa.Boolean(), nullable=True, comment="連結の短信か。解析するまで NULL"
        ),
    )
    op.add_column(
        "tdnet_disclosures",
        sa.Column(
            "fiscal_year_end",
            sa.Date(),
            nullable=True,
            comment="表紙から読んだ会計年度末。どの期の短信かを表す。解析するまで NULL",
        ),
    )
    op.add_column(
        "tdnet_disclosures",
        sa.Column(
            "quarter",
            sa.String(2),
            nullable=True,
            comment="四半期の区分 (Q1 / Q2 / Q3 / FY)。解析するまで NULL",
        ),
    )
    op.add_column(
        "tdnet_disclosures",
        sa.Column(
            "parsed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="iXBRL を解析した日時。NULL なら未解析で、次の実行が拾い直す",
        ),
    )
    op.add_column(
        "tdnet_disclosures",
        sa.Column(
            "parse_error",
            sa.Text(),
            nullable=True,
            comment="解析に失敗した理由。次の実行で解析し直すと消える。成功した書類は NULL",
        ),
    )

    op.create_index(
        "ix_tdnet_disclosures_code_fiscal_year_end",
        "tdnet_disclosures",
        ["code", "fiscal_year_end"],
    )

    op.create_table(
        "tdnet_summary_facts",
        sa.Column(
            "doc_id",
            sa.String(length=24),
            nullable=False,
            comment="TDnet 書類 ID (FK: tdnet_disclosures.doc_id)",
        ),
        sa.Column(
            "concept",
            sa.String(length=256),
            nullable=False,
            comment="要素名 (例: tse-ed-t_NetSales)。表紙は tse-ed-t だけで会社独自の拡張は無い",
        ),
        sa.Column(
            "context_ref",
            sa.String(length=256),
            nullable=False,
            comment="原文の context の id。軸に割る前のもの",
        ),
        sa.Column(
            "scope",
            sa.String(length=8),
            nullable=False,
            comment="Current (当期) / Prior (前期) / Next (来期)",
        ),
        sa.Column(
            "period_kind",
            sa.String(length=16),
            nullable=False,
            comment="Year (通期) / AccumulatedQ1〜Q3 (四半期累計) など",
        ),
        sa.Column(
            "quarter_member",
            sa.String(length=32),
            nullable=True,
            comment="配当の内訳に付く FirstQuarter / YearEnd / Annual など。無ければ NULL",
        ),
        sa.Column(
            "is_consolidated",
            sa.Boolean(),
            nullable=True,
            comment="連結の値か。context に区分が無ければ NULL",
        ),
        sa.Column(
            "fact_type",
            sa.String(length=16),
            nullable=True,
            comment="Result (実績) / Forecast (会社予想) / Upper・Lower (予想レンジの上下限)",
        ),
        sa.Column(
            "period_type",
            sa.String(length=8),
            nullable=False,
            comment="duration (期間) か instant (時点) か",
        ),
        sa.Column(
            "period_start", sa.Date(), nullable=True, comment="期間の開始日。instant では NULL"
        ),
        sa.Column(
            "period_end", sa.Date(), nullable=False, comment="期間の末日、または時点の日付"
        ),
        sa.Column(
            "value",
            sa.Numeric(),
            nullable=False,
            comment="値。scale を掛けたあとの数。金額は円、比率は小数のまま",
        ),
        sa.Column(
            "unit",
            sa.String(length=24),
            nullable=True,
            comment="単位 (JPY / Pure / JPYPerShares / Shares)",
        ),
        sa.Column(
            "decimals",
            sa.String(length=8),
            nullable=True,
            comment="原文の精度表示。値のスケールとは関係しない",
        ),
        *_TIMESTAMPS,
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["tdnet_disclosures.doc_id"],
            name="fk_tdnet_summary_facts_doc_id_tdnet_disclosures",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("doc_id", "concept", "context_ref", name="pk_tdnet_summary_facts"),
        comment="決算短信の表紙の数値 (実績と会社予想)",
    )
    op.create_index(
        "ix_tdnet_summary_facts_concept_period_end",
        "tdnet_summary_facts",
        ["concept", "period_end"],
    )
    op.create_index("ix_tdnet_summary_facts_fact_type", "tdnet_summary_facts", ["fact_type"])

    op.create_table(
        "tdnet_statement_facts",
        sa.Column(
            "doc_id",
            sa.String(length=24),
            nullable=False,
            comment="TDnet 書類 ID (FK: tdnet_disclosures.doc_id)",
        ),
        sa.Column(
            "ordinal",
            sa.SmallInteger(),
            nullable=False,
            comment="書類を通した通し番号。計算書に刷られる順になる",
        ),
        sa.Column(
            "section",
            sa.String(length=4),
            nullable=False,
            comment="計算書。BS / PL / PC / CI / CF / SS / SG。IFRS の財政状態計算書は BS に寄せる",
        ),
        sa.Column(
            "concept",
            sa.String(length=512),
            nullable=False,
            comment="要素名 (例: jppfs_cor_NetSales)。有報と同じ体系で、会社独自の拡張も入る",
        ),
        sa.Column(
            "context_ref",
            sa.String(length=512),
            nullable=False,
            comment="XBRL の context の id。期間と区分を指す",
        ),
        sa.Column(
            "term",
            sa.String(length=1),
            nullable=False,
            comment="どの期の短信か。a (通期) / q (四半期) / s (中間)",
        ),
        sa.Column(
            "is_consolidated",
            sa.Boolean(),
            nullable=False,
            comment="連結の計算書か。ファイル名の 2 文字目で決まる",
        ),
        sa.Column(
            "member",
            sa.String(length=512),
            nullable=True,
            comment="context_ref から期間の部分を除いた残り。全体の値は NULL",
        ),
        sa.Column(
            "period_type",
            sa.String(length=8),
            nullable=False,
            comment="duration (期間) か instant (時点) か",
        ),
        sa.Column(
            "period_start", sa.Date(), nullable=True, comment="期間の開始日。instant では NULL"
        ),
        sa.Column(
            "period_end", sa.Date(), nullable=False, comment="期間の末日、または時点の日付"
        ),
        sa.Column(
            "value",
            sa.Numeric(),
            nullable=False,
            comment="値。円のまま入れる。丸めるのは表示側の仕事",
        ),
        sa.Column(
            "unit",
            sa.String(length=24),
            nullable=True,
            comment="単位 (JPY / Pure / JPYPerShares / Shares)",
        ),
        sa.Column(
            "decimals",
            sa.String(length=8),
            nullable=True,
            comment="原文の精度表示。値のスケールとは関係しない",
        ),
        *_TIMESTAMPS,
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["tdnet_disclosures.doc_id"],
            name="fk_tdnet_statement_facts_doc_id_tdnet_disclosures",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("doc_id", "ordinal", name="pk_tdnet_statement_facts"),
        comment="決算短信の添付にある財務諸表の数値 (1 行 1 数値)",
    )
    op.create_index(
        "ix_tdnet_statement_facts_concept_period_end",
        "tdnet_statement_facts",
        ["concept", "period_end"],
    )
    op.create_index(
        "ix_tdnet_statement_facts_period_end", "tdnet_statement_facts", ["period_end"]
    )


def downgrade() -> None:
    op.drop_index("ix_tdnet_statement_facts_period_end", table_name="tdnet_statement_facts")
    op.drop_index(
        "ix_tdnet_statement_facts_concept_period_end", table_name="tdnet_statement_facts"
    )
    op.drop_table("tdnet_statement_facts")
    op.drop_index("ix_tdnet_summary_facts_fact_type", table_name="tdnet_summary_facts")
    op.drop_index("ix_tdnet_summary_facts_concept_period_end", table_name="tdnet_summary_facts")
    op.drop_table("tdnet_summary_facts")
    op.drop_index("ix_tdnet_disclosures_code_fiscal_year_end", table_name="tdnet_disclosures")
    for name in (
        "parse_error",
        "parsed_at",
        "quarter",
        "fiscal_year_end",
        "is_consolidated",
        "accounting_standard",
    ):
        op.drop_column("tdnet_disclosures", name)
