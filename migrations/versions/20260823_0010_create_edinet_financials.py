"""ファクトを名寄せした財務項目の置き場を作る.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-23

``edinet_facts`` は XBRL の要素名のままなので、会社をまたいで並べられない。売上高は日本
GAAP が ``jppfs_cor_NetSales``、IFRS が ``jpigp_cor_RevenueIFRS``、鉄道会社は
``jppfs_cor_OperatingRevenueRWY`` になる。共通の ``item`` に寄せた層をここに置く。

第 1 弾は 6 項目 (売上高・営業利益・経常利益・当期純利益・総資産・純資産)。これで営業
利益率・総資産回転率・ROA・ROE の 3 分解・売上成長率まで届く。1 株当たりの値やキャッシュ
フローは、見ると決めたときに足す。

``source_section`` と ``source_concept`` を必ず持たせる。金融業の経常収益を売上高に寄せる
ような判断が入るため、出所を捨てると値がおかしいときに切り分けられない。

適用したあとに `kabu normalize financials` を流すと埋まる。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LATEST_FINANCIALS_VIEW = """
CREATE VIEW edinet_latest_financials AS
SELECT DISTINCT ON (d.code, f.item, f.period_end)
    d.code,
    f.item,
    f.period_end,
    f.period_start,
    f.value,
    f.unit,
    f.doc_id,
    d.fiscal_year_end,
    d.accounting_standard,
    d.is_consolidated,
    f.source_section,
    f.source_concept
FROM edinet_financials f
JOIN edinet_documents d ON d.doc_id = f.doc_id
WHERE d.fiscal_year_end IS NOT NULL
ORDER BY d.code, f.item, f.period_end, d.fiscal_year_end DESC, d.submit_date DESC, f.doc_id DESC
"""
"""銘柄・項目・期ごとに 1 行だけを残したビュー.

同じ期の売上高が複数の書類から出てくる。「主要な経営指標等」は 5 期分を載せるため、
2026 年の有報にも 2022 年の売上高が入っている。訂正有報も別の行として並ぶ。

会計年度末がいちばん新しい書類を採る。その期を当期として書いた書類の値になり、
訂正があればそれが選ばれる。過去 4 期分として遡って載った値は、当期として書かれた
値がある限り使わない。
"""


def upgrade() -> None:
    op.create_table(
        "edinet_financials",
        sa.Column(
            "doc_id",
            sa.String(length=16),
            nullable=False,
            comment="EDINET 書類管理番号 (FK: edinet_documents.doc_id)",
        ),
        sa.Column(
            "item",
            sa.String(length=32),
            nullable=False,
            comment="財務項目 (net_sales/operating_income/ordinary_income/net_income/"
            "total_assets/net_assets)",
        ),
        sa.Column(
            "period_end",
            sa.Date(),
            nullable=False,
            comment="期間の末日、または時点の日付",
        ),
        sa.Column(
            "period_start",
            sa.Date(),
            nullable=True,
            comment="期間の開始日。時点の項目 (総資産・純資産) では NULL",
        ),
        sa.Column(
            "value",
            sa.Numeric(),
            nullable=False,
            comment="値。円のまま入れる。丸めるのは表示側の仕事",
        ),
        sa.Column(
            "unit",
            sa.String(length=16),
            nullable=True,
            comment="単位 (JPY)。元のファクトのものをそのまま持つ",
        ),
        sa.Column(
            "source_section",
            sa.String(length=8),
            nullable=False,
            comment="どこから取ったか。BR: 経営指標の推移 / PL: 損益計算書 / BS: 貸借対照表",
        ),
        sa.Column(
            "source_concept",
            sa.String(length=512),
            nullable=False,
            comment="元の XBRL 要素名。名寄せの判断を後から検証するために必ず残す",
        ),
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
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["edinet_documents.doc_id"],
            name="fk_edinet_financials_doc_id_edinet_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("doc_id", "item", "period_end", name="pk_edinet_financials"),
        comment="有価証券報告書のファクトを共通の財務項目に名寄せした値",
    )
    op.create_index(
        "ix_edinet_financials_item_period_end",
        "edinet_financials",
        ["item", "period_end"],
    )
    op.execute(_LATEST_FINANCIALS_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS edinet_latest_financials")
    op.drop_index("ix_edinet_financials_item_period_end", table_name="edinet_financials")
    op.drop_table("edinet_financials")
