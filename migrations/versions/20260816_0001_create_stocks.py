"""stocks と stock_snapshots を作る.

Revision ID: 0001
Revises:
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MARKET_SEGMENT_CHECK = "market_segment IN ('prime', 'standard', 'growth')"


def upgrade() -> None:
    op.create_table(
        "stocks",
        sa.Column(
            "code",
            sa.String(length=5),
            nullable=False,
            comment="JPX 銘柄コード。4 桁英数字が基本だが優先株・種類株は 5 桁",
        ),
        sa.Column("name", sa.String(length=64), nullable=False, comment="銘柄名"),
        sa.Column(
            "market_segment",
            sa.String(length=16),
            nullable=False,
            comment="市場区分 (prime / standard / growth)",
        ),
        sa.Column(
            "industry33_code",
            sa.String(length=4),
            nullable=False,
            comment="東証 33 業種コード (0 埋め 4 桁)",
        ),
        sa.Column(
            "industry33_name", sa.String(length=32), nullable=False, comment="東証 33 業種区分"
        ),
        sa.Column(
            "industry17_code",
            sa.String(length=2),
            nullable=False,
            comment="東証 17 業種コード (0 埋め 2 桁)",
        ),
        sa.Column(
            "industry17_name", sa.String(length=32), nullable=False, comment="東証 17 業種区分"
        ),
        sa.Column(
            "topix_scale_code",
            sa.String(length=1),
            nullable=True,
            comment="TOPIX 規模コード。対象外は NULL",
        ),
        sa.Column(
            "topix_scale_name",
            sa.String(length=32),
            nullable=True,
            comment="TOPIX 規模区分。対象外は NULL",
        ),
        sa.Column(
            "base_date", sa.Date(), nullable=False, comment="この行の元になった JPX 一覧の基準日"
        ),
        sa.Column(
            "is_listed",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="上場中なら true",
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
        sa.CheckConstraint(_MARKET_SEGMENT_CHECK, name="market_segment"),
        sa.PrimaryKeyConstraint("code"),
        comment="銘柄マスタ (JPX 東証上場銘柄一覧の最新状態)",
    )
    op.create_index("ix_stocks_market_segment", "stocks", ["market_segment"])
    op.create_index("ix_stocks_industry33_code", "stocks", ["industry33_code"])

    op.create_table(
        "stock_snapshots",
        sa.Column(
            "base_date",
            sa.Date(),
            nullable=False,
            comment="JPX 一覧の基準日 (ファイルの「日付」列)",
        ),
        sa.Column("code", sa.String(length=5), nullable=False, comment="JPX 銘柄コード"),
        sa.Column("name", sa.String(length=64), nullable=False, comment="銘柄名"),
        sa.Column(
            "market_segment",
            sa.String(length=16),
            nullable=False,
            comment="市場区分 (prime / standard / growth)",
        ),
        sa.Column(
            "industry33_code",
            sa.String(length=4),
            nullable=False,
            comment="東証 33 業種コード (0 埋め 4 桁)",
        ),
        sa.Column(
            "industry33_name", sa.String(length=32), nullable=False, comment="東証 33 業種区分"
        ),
        sa.Column(
            "industry17_code",
            sa.String(length=2),
            nullable=False,
            comment="東証 17 業種コード (0 埋め 2 桁)",
        ),
        sa.Column(
            "industry17_name", sa.String(length=32), nullable=False, comment="東証 17 業種区分"
        ),
        sa.Column(
            "topix_scale_code",
            sa.String(length=1),
            nullable=True,
            comment="TOPIX 規模コード。対象外は NULL",
        ),
        sa.Column(
            "topix_scale_name",
            sa.String(length=32),
            nullable=True,
            comment="TOPIX 規模区分。対象外は NULL",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="行の作成日時",
        ),
        sa.CheckConstraint(_MARKET_SEGMENT_CHECK, name="market_segment"),
        sa.ForeignKeyConstraint(["code"], ["stocks.code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("base_date", "code"),
        comment="銘柄一覧の基準日ごとのスナップショット",
    )
    op.create_index("ix_stock_snapshots_code_base_date", "stock_snapshots", ["code", "base_date"])


def downgrade() -> None:
    op.drop_index("ix_stock_snapshots_code_base_date", table_name="stock_snapshots")
    op.drop_table("stock_snapshots")
    op.drop_index("ix_stocks_industry33_code", table_name="stocks")
    op.drop_index("ix_stocks_market_segment", table_name="stocks")
    op.drop_table("stocks")
