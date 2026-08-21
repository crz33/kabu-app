"""edinet_documents を作る.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "edinet_documents",
        sa.Column(
            "doc_id",
            sa.String(length=16),
            nullable=False,
            comment="EDINET 書類管理番号 (例: S100YW7F)",
        ),
        sa.Column(
            "edinet_code",
            sa.String(length=8),
            nullable=False,
            comment="提出者の EDINET コード (例: E05729)。商号変更や上場廃止をまたいで変わらない",
        ),
        sa.Column(
            "sec_code",
            sa.String(length=5),
            nullable=True,
            comment="EDINET の証券コード。末尾 0 埋めの 5 桁",
        ),
        sa.Column(
            "code",
            sa.String(length=5),
            nullable=False,
            comment="JPX 銘柄コード。sec_code の末尾 0 を落としたもの",
        ),
        sa.Column(
            "doc_type_code",
            sa.String(length=3),
            nullable=False,
            comment="書類種別コード (120: 有報 / 130: 訂正有報)",
        ),
        sa.Column(
            "parent_doc_id",
            sa.String(length=16),
            nullable=True,
            comment="訂正報告書が訂正する対象の書類管理番号。有報自身は NULL",
        ),
        sa.Column(
            "submit_date",
            sa.Date(),
            nullable=False,
            comment="提出日。書類一覧 API を引いた日付と一致する",
        ),
        sa.Column(
            "submitted_at",
            sa.DateTime(),
            nullable=True,
            comment="提出日時 (JST)。分までしか無い",
        ),
        sa.Column(
            "period_end",
            sa.Date(),
            nullable=True,
            comment="対象期間の末日 (決算日)。書類によっては入らない",
        ),
        sa.Column("filer_name", sa.String(length=200), nullable=False, comment="提出者名"),
        sa.Column("doc_description", sa.String(length=500), nullable=False, comment="書類の説明"),
        sa.Column(
            "has_xbrl",
            sa.Boolean(),
            nullable=False,
            comment="XBRL 一式の ZIP を取得できるか (xbrlFlag)",
        ),
        sa.Column(
            "is_withdrawn",
            sa.Boolean(),
            nullable=False,
            comment="取り下げられた書類か (withdrawalStatus)。true なら本文を取りに行かない",
        ),
        sa.Column(
            "downloaded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="ZIP を保存した日時。NULL なら未取得で、次の実行が拾い直す",
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
        sa.ForeignKeyConstraint(["code"], ["stocks.code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("doc_id"),
        comment="EDINET 提出書類のメタデータ (有価証券報告書とその訂正)",
    )
    op.create_index("ix_edinet_documents_submit_date", "edinet_documents", ["submit_date"])
    op.create_index(
        "ix_edinet_documents_code_period_end", "edinet_documents", ["code", "period_end"]
    )


def downgrade() -> None:
    op.drop_index("ix_edinet_documents_code_period_end", table_name="edinet_documents")
    op.drop_index("ix_edinet_documents_submit_date", table_name="edinet_documents")
    op.drop_table("edinet_documents")
