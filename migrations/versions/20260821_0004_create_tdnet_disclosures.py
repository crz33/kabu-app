"""tdnet_disclosures を作る.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tdnet_disclosures",
        sa.Column(
            "doc_id",
            sa.String(length=24),
            nullable=False,
            comment="TDnet 書類 ID。PDF のファイル名から拡張子を除いたもの",
        ),
        sa.Column("disclosed_date", sa.Date(), nullable=False, comment="開示日"),
        sa.Column(
            "disclosed_time",
            sa.Time(),
            nullable=False,
            comment="開示時刻。一覧には分までしか出ない",
        ),
        sa.Column(
            "sec_code",
            sa.String(length=5),
            nullable=True,
            comment="TDnet の証券コード。末尾 0 埋めの 5 桁。findocgen から移した行は NULL",
        ),
        sa.Column(
            "code",
            sa.String(length=5),
            nullable=False,
            comment="JPX 銘柄コード。sec_code の末尾 0 を落としたもの。上場廃止した銘柄も入る",
        ),
        sa.Column("company_name", sa.String(length=200), nullable=False, comment="開示した会社名"),
        sa.Column("title", sa.String(length=500), nullable=False, comment="開示の表題"),
        sa.Column(
            "markets",
            sa.String(length=16),
            nullable=True,
            comment="上場市場の略号 (例: 東札福)。東証以外の単独上場もある。移した行は NULL",
        ),
        sa.Column(
            "is_amendment",
            sa.Boolean(),
            nullable=False,
            comment="訂正の開示か。表題に「訂正」が入るかで判定する",
        ),
        sa.Column(
            "xbrl_file",
            sa.String(length=64),
            nullable=True,
            comment="XBRL の ZIP のファイル名。NULL なら XBRL が無く、PDF だけが本体になる",
        ),
        sa.Column(
            "downloaded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="実体を保存した日時。NULL なら未取得。31 日を過ぎると取れなくなる",
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
        sa.PrimaryKeyConstraint("doc_id"),
        comment="TDnet の適時開示のメタデータ (決算短信とその訂正)",
    )
    op.create_index("ix_tdnet_disclosures_disclosed_date", "tdnet_disclosures", ["disclosed_date"])
    op.create_index(
        "ix_tdnet_disclosures_code_disclosed_date", "tdnet_disclosures", ["code", "disclosed_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_tdnet_disclosures_code_disclosed_date", table_name="tdnet_disclosures")
    op.drop_index("ix_tdnet_disclosures_disclosed_date", table_name="tdnet_disclosures")
    op.drop_table("tdnet_disclosures")
