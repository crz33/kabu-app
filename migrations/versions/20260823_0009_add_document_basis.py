"""会計基準・連結の有無・会計年度の開始日を書類に持たせる.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-23

数値を読むのに要る情報を DEI から拾って列に置く。同じ勘定でも連結と単体では意味が違い、
会計基準が変われば使う要素名も変わる。

ファクト側から推測はできるが、当てにしない。US GAAP は日本 GAAP と要素の名前空間が同じで
見分けられず (880 書類の実測では US GAAP が 1 件も出ず、判別条件を確かめられなかった)、
IFRS でも jppfs の要素が混ざる書類がある。連結の有無を BR_C セクションの有無から逆算する
のも、パーサのセクション割り当てを変えると壊れる。

既存行は NULL のままになる。埋めるには kabu parse edinet --reparse を流す。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "edinet_documents",
        sa.Column(
            "accounting_standard",
            sa.String(length=16),
            nullable=True,
            comment="会計基準 (Japan GAAP / IFRS / US GAAP)。解析するまで NULL",
        ),
    )
    op.add_column(
        "edinet_documents",
        sa.Column(
            "is_consolidated",
            sa.Boolean(),
            nullable=True,
            comment="連結財務諸表を作る会社か。false なら数値はすべて単体。解析するまで NULL",
        ),
    )
    op.add_column(
        "edinet_documents",
        sa.Column(
            "fiscal_year_start",
            sa.Date(),
            nullable=True,
            comment="会計年度の開始日。fiscal_year_end との差で期の長さが分かる",
        ),
    )


def downgrade() -> None:
    op.drop_column("edinet_documents", "fiscal_year_start")
    op.drop_column("edinet_documents", "is_consolidated")
    op.drop_column("edinet_documents", "accounting_standard")
