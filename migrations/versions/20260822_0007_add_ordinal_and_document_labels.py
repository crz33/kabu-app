"""表示順と階層を持たせ、書類ごとのラベルを分ける.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-22

edinet_labels に書類同梱のラベルを混ぜていたのをやめる。会社は標準の勘定に独自の言い換えを
付けるため、全社で 1 行にまとめると他社の文言が出てしまう。既存の行は混ざっているので消す。
タクソノミから作り直せるので、適用後に `kabu parse taxonomy` を流すこと。

edinet_facts の ordinal と depth は既存行を埋められない。解析し直せば入るので、いったん 0 を
既定値にして列を足し、既定値だけ外す。適用後に `kabu parse edinet --reparse` を流すこと。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENT_VIEW = """
CREATE VIEW edinet_statement_lines AS
SELECT
    f.doc_id,
    d.code,
    d.filer_name,
    f.section,
    f.ordinal,
    f.depth,
    f.concept,
    COALESCE(dl.label, l.label) AS label,
    f.member,
    f.period_type,
    f.period_start,
    f.period_end,
    f.value,
    f.unit
FROM edinet_facts f
JOIN edinet_documents d ON d.doc_id = f.doc_id
LEFT JOIN edinet_document_labels dl ON dl.doc_id = f.doc_id AND dl.concept = f.concept
LEFT JOIN edinet_labels l ON l.concept = f.concept
"""
"""計算書を刷られた形で読むためのビュー.

``ORDER BY section, ordinal`` で並べ、``depth`` でインデントすると有報の表になる。
ラベルは書類に同梱されたものを優先し、無ければタクソノミの標準ラベルに落とす。
"""


def upgrade() -> None:
    op.add_column(
        "edinet_facts",
        sa.Column(
            "ordinal",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
            comment="計算書に刷られる順。表示リンクを深さ優先でたどった通し番号",
        ),
    )
    op.add_column(
        "edinet_facts",
        sa.Column(
            "depth",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
            comment="階層の深さ。0 が計算書そのもので、勘定は 1 以上",
        ),
    )
    op.alter_column("edinet_facts", "ordinal", server_default=None)
    op.alter_column("edinet_facts", "depth", server_default=None)

    op.create_table(
        "edinet_document_labels",
        sa.Column(
            "doc_id",
            sa.String(length=16),
            nullable=False,
            comment="EDINET 書類管理番号 (FK: edinet_documents.doc_id)",
        ),
        sa.Column("concept", sa.String(length=512), nullable=False, comment="XBRL の要素名"),
        sa.Column(
            "label",
            sa.String(length=500),
            nullable=False,
            comment="この書類でのラベル。標準ラベルより優先する",
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
            name="fk_edinet_document_labels_doc_id_edinet_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("doc_id", "concept"),
        comment="有報に同梱されていた、その書類での要素名のラベル",
    )

    # 書類同梱のラベルが混ざっている。タクソノミから作り直す
    op.execute("DELETE FROM edinet_labels")
    op.drop_column("edinet_labels", "source")
    op.execute("COMMENT ON TABLE edinet_labels IS '金融庁のタクソノミが定める要素名の標準ラベル'")

    op.execute(_STATEMENT_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS edinet_statement_lines")
    op.add_column(
        "edinet_labels",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="taxonomy",
            comment="どこから読んだか。taxonomy: 金融庁のタクソノミ / document: 書類に同梱",
        ),
    )
    op.alter_column("edinet_labels", "source", server_default=None)
    op.execute("COMMENT ON TABLE edinet_labels IS 'XBRL の要素名に対する日本語ラベル'")
    op.drop_table("edinet_document_labels")
    op.drop_column("edinet_facts", "depth")
    op.drop_column("edinet_facts", "ordinal")
