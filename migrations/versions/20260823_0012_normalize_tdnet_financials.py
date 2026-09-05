"""決算短信の添付を名寄せした財務項目の置き場を作る.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-23

添付は jppfs_cor / jpigp_cor と有報とまったく同じ体系なので、項目の定義は
``normalizers.financials.ITEM_SPECS`` を共有する。実測では PL / BS のエントリ 18 個のうち
15 個が短信でも当たり、売上の取りこぼしは無かった。有報が年 1 回なのに対し、こちらは
四半期ごとに入るので粒度が上がる。

有報と違うのは ``period_kind`` を持つこと。短信は同じ期末に年初来累計と単独四半期が並ぶ。
「Q2 累計 100 億」と「Q2 単独 50 億」を取り違えると致命的なので、主キーに含める。
``period_start`` から期間の長さを計算しても大半は判別できるが、決算期を変えた会社で狂う。
実際に「第５四半期決算短信」を出す会社がある。context に書いてあるものを読む。

適用したあとに `kabu normalize tdnet-financials` を流すと埋まる。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tdnet_financials",
        sa.Column(
            "doc_id",
            sa.String(length=24),
            nullable=False,
            comment="TDnet 書類 ID (FK: tdnet_disclosures.doc_id)",
        ),
        sa.Column(
            "item",
            sa.String(length=32),
            nullable=False,
            comment="財務項目 (net_sales/operating_income/ordinary_income/net_income/"
            "total_assets/net_assets)。有報の edinet_financials と同じ",
        ),
        sa.Column(
            "period_kind",
            sa.String(length=16),
            nullable=False,
            comment="期の種類。ytd (年初来累計) / quarter (単独四半期) / year (通期) / "
            "interim (中間) と、時点の quarter_end / year_end / interim_end",
        ),
        sa.Column("period_end", sa.Date(), nullable=False, comment="期間の末日、または時点の日付"),
        sa.Column(
            "period_start",
            sa.Date(),
            nullable=True,
            comment="期間の開始日。時点の項目では NULL",
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
            comment="単位 (JPY)。元のファクトのものをそのまま持つ",
        ),
        sa.Column(
            "source_section",
            sa.String(length=4),
            nullable=False,
            comment="どこから取ったか。PL: 損益計算書 / BS: 貸借対照表",
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
            ["tdnet_disclosures.doc_id"],
            name="fk_tdnet_financials_doc_id_tdnet_disclosures",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "doc_id", "item", "period_kind", "period_end", name="pk_tdnet_financials"
        ),
        comment="決算短信の添付を共通の財務項目に名寄せした値",
    )
    op.create_index(
        "ix_tdnet_financials_item_period_end", "tdnet_financials", ["item", "period_end"]
    )
    op.create_index("ix_tdnet_financials_period_kind", "tdnet_financials", ["period_kind"])


def downgrade() -> None:
    op.drop_index("ix_tdnet_financials_period_kind", table_name="tdnet_financials")
    op.drop_index("ix_tdnet_financials_item_period_end", table_name="tdnet_financials")
    op.drop_table("tdnet_financials")
