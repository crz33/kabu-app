"""ticks を作る.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRICE = sa.Numeric(12, 2)


def upgrade() -> None:
    op.create_table(
        "ticks",
        sa.Column(
            "code",
            sa.String(length=8),
            nullable=False,
            comment="JPX 銘柄コード。市場指数は 998405 のような 6 桁",
        ),
        sa.Column("date", sa.Date(), nullable=False, comment="取引日"),
        sa.Column("open", _PRICE, nullable=False, comment="始値 (円)"),
        sa.Column("high", _PRICE, nullable=False, comment="高値 (円)"),
        sa.Column("low", _PRICE, nullable=False, comment="安値 (円)"),
        sa.Column("close", _PRICE, nullable=False, comment="終値 (円)"),
        sa.Column(
            "volume",
            sa.BigInteger(),
            nullable=False,
            comment="出来高 (株)。6 億を超える日があるので 8 バイトで持つ",
        ),
        sa.Column(
            "adjusted_close",
            _PRICE,
            nullable=True,
            comment="株式分割を調整した終値。findocgen から移した行は NULL",
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
        sa.PrimaryKeyConstraint("code", "date"),
        comment="日次の四本値と出来高 (Yahoo Finance)",
    )
    op.create_index("ix_ticks_date", "ticks", ["date"])


def downgrade() -> None:
    op.drop_index("ix_ticks_date", table_name="ticks")
    op.drop_table("ticks")
