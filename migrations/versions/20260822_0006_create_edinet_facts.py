"""edinet_facts, edinet_labels, edinet_shareholders を作り、解析の記録を足す.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
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
        "edinet_documents",
        sa.Column(
            "parsed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="XBRL を解析した日時。NULL なら未解析で、次の実行が拾い直す",
        ),
    )
    op.add_column(
        "edinet_documents",
        sa.Column(
            "parse_error",
            sa.Text(),
            nullable=True,
            comment="解析に失敗した理由。次の実行で解析し直すと消える。成功した書類は NULL",
        ),
    )

    op.create_table(
        "edinet_facts",
        sa.Column(
            "doc_id",
            sa.String(length=16),
            nullable=False,
            comment="EDINET 書類管理番号 (FK: edinet_documents.doc_id)",
        ),
        sa.Column(
            "section",
            sa.String(length=8),
            nullable=False,
            comment="どの計算書か。BR: 経営指標の推移 / BR_C: 同 提出会社 / BS / PL / CS",
        ),
        sa.Column(
            "concept",
            sa.String(length=512),
            nullable=False,
            comment="XBRL の要素名 (例: jppfs_cor_OperatingIncome)。会社独自の拡張も入る",
        ),
        sa.Column(
            "context_ref",
            sa.String(length=512),
            nullable=False,
            comment="XBRL の context の id。期間と区分を指す (例: CurrentYearDuration)",
        ),
        sa.Column(
            "member",
            sa.String(length=512),
            nullable=True,
            comment="context_ref から期間の部分を除いた残り。連結全体の値は NULL",
        ),
        sa.Column(
            "period_type",
            sa.String(length=8),
            nullable=False,
            comment="duration (期間) か instant (時点) か",
        ),
        sa.Column(
            "period_start",
            sa.Date(),
            nullable=True,
            comment="期間の開始日。instant では NULL",
        ),
        sa.Column(
            "period_end",
            sa.Date(),
            nullable=False,
            comment="期間の末日、または時点の日付",
        ),
        sa.Column(
            "value",
            sa.Numeric(),
            nullable=False,
            comment="値。金額は円、株数は株、比率は小数のまま",
        ),
        sa.Column(
            "unit",
            sa.String(length=16),
            nullable=True,
            comment="単位 (JPY / shares / pure / JPYPerShares)",
        ),
        sa.Column(
            "decimals",
            sa.String(length=8),
            nullable=True,
            comment="原文の精度表示 (-6 なら百万円の位まで有効)",
        ),
        *_TIMESTAMPS,
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["edinet_documents.doc_id"],
            name="fk_edinet_facts_doc_id_edinet_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("doc_id", "section", "concept", "context_ref"),
        comment="有価証券報告書の XBRL から取り出した財務諸表の数値 (1 行 1 数値)",
    )
    op.create_index("ix_edinet_facts_concept_period_end", "edinet_facts", ["concept", "period_end"])
    op.create_index("ix_edinet_facts_period_end", "edinet_facts", ["period_end"])

    op.create_table(
        "edinet_labels",
        sa.Column(
            "concept",
            sa.String(length=512),
            nullable=False,
            comment="XBRL の要素名 (例: jppfs_cor_OperatingIncome)",
        ),
        sa.Column("label", sa.String(length=500), nullable=False, comment="日本語の標準ラベル"),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            comment="どこから読んだか。taxonomy: 金融庁のタクソノミ / document: 書類に同梱",
        ),
        *_TIMESTAMPS,
        sa.PrimaryKeyConstraint("concept"),
        comment="XBRL の要素名に対する日本語ラベル",
    )

    op.create_table(
        "edinet_shareholders",
        sa.Column(
            "doc_id",
            sa.String(length=16),
            nullable=False,
            comment="EDINET 書類管理番号 (FK: edinet_documents.doc_id)",
        ),
        sa.Column("rank", sa.SmallInteger(), nullable=False, comment="大株主の順位。1 が筆頭"),
        sa.Column("code", sa.String(length=5), nullable=False, comment="JPX 銘柄コード"),
        sa.Column(
            "period_end",
            sa.Date(),
            nullable=True,
            comment="対象期間の末日。書類によっては入らない",
        ),
        sa.Column("name", sa.String(length=200), nullable=False, comment="株主名。原文のまま"),
        sa.Column("shares", sa.BigInteger(), nullable=True, comment="所有株式数 (株)"),
        sa.Column(
            "ratio",
            sa.Numeric(7, 2),
            nullable=True,
            comment="発行済株式総数に対する保有割合 (%)。XBRL の小数を 100 倍したもの",
        ),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            comment="分類 (individual/director/asset_mgmt/employee/trust/public/corporate)",
        ),
        sa.Column(
            "is_owner",
            sa.Boolean(),
            nullable=False,
            comment="オーナー系株主か。owner_ratio を出すときの合算対象",
        ),
        *_TIMESTAMPS,
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["edinet_documents.doc_id"],
            name="fk_edinet_shareholders_doc_id_edinet_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("doc_id", "rank"),
        comment="有価証券報告書「大株主の状況」の上位株主とオーナー判定",
    )
    op.create_index(
        "ix_edinet_shareholders_code_period_end",
        "edinet_shareholders",
        ["code", "period_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_edinet_shareholders_code_period_end", table_name="edinet_shareholders")
    op.drop_table("edinet_shareholders")
    op.drop_table("edinet_labels")
    op.drop_index("ix_edinet_facts_period_end", table_name="edinet_facts")
    op.drop_index("ix_edinet_facts_concept_period_end", table_name="edinet_facts")
    op.drop_table("edinet_facts")
    op.drop_column("edinet_documents", "parse_error")
    op.drop_column("edinet_documents", "parsed_at")
