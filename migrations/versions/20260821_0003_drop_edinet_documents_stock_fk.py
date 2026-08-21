"""edinet_documents から stocks への外部キーを外す.

上場廃止した会社の有報も残すため。stocks は JPX の最新一覧から作るので、買収や MBO で
消えた会社は載らない。外部キーがあると、そうした会社の書類を捨てるしかなくなり、
過去を評価するときに生存者バイアスが入る。

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = "JPX 銘柄コード。sec_code の末尾 0 を落としたもの。上場廃止した銘柄も入る"
_OLD_COMMENT = "JPX 銘柄コード。sec_code の末尾 0 を落としたもの"


def upgrade() -> None:
    op.drop_constraint("fk_edinet_documents_code_stocks", "edinet_documents", type_="foreignkey")
    op.alter_column(
        "edinet_documents",
        "code",
        existing_type=sa.String(length=5),
        existing_nullable=False,
        comment=_COMMENT,
        existing_comment=_OLD_COMMENT,
    )


def downgrade() -> None:
    op.alter_column(
        "edinet_documents",
        "code",
        existing_type=sa.String(length=5),
        existing_nullable=False,
        comment=_OLD_COMMENT,
        existing_comment=_COMMENT,
    )
    op.create_foreign_key(
        "fk_edinet_documents_code_stocks",
        "edinet_documents",
        "stocks",
        ["code"],
        ["code"],
        ondelete="RESTRICT",
    )
