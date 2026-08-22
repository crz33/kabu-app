"""訂正有報も解析対象にし、期ごとの最新版を引けるようにする.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-23

訂正有価証券報告書 (130) は様式が有報と同じで、同じパーサでそのまま読める。585 件を試して
全件が解析でき、74 組 (12.6%) で純資産・EPS・ROE といった数値が動いていた。取り込まないと
その分だけ古い数値が残る。

訂正報告書は差分ではなく全文になる (585 組すべてでファクト数の比が 0.99〜1.02)。だから元と
マージせず、期ごとに最新の書類を採ればよい。マージはむしろ誤りで、赤字転落で消えた PER を
元から拾い直すことになる。

どの期の書類かは XBRL の DEI から読む。API は 130 に periodEnd を返さない。parent_doc_id を
辿る手もあるが、元の有報が DB に無い訂正が 1,180 件中 595 件あるため期が決まらない。DEI の
会計年度末は有報 120 件で API の periodEnd と完全に一致し、訂正 80 件でも全件取れている。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LATEST_FACTS_VIEW = """
CREATE VIEW edinet_latest_facts AS
SELECT
    f.doc_id,
    d.code,
    d.fiscal_year_end,
    f.section,
    f.ordinal,
    f.depth,
    f.concept,
    f.member,
    f.period_type,
    f.period_start,
    f.period_end,
    f.value,
    f.unit,
    f.decimals
FROM edinet_facts f
JOIN edinet_documents d ON d.doc_id = f.doc_id
WHERE f.doc_id IN (
    SELECT DISTINCT ON (code, fiscal_year_end) doc_id
    FROM edinet_documents
    WHERE parsed_at IS NOT NULL AND fiscal_year_end IS NOT NULL
    ORDER BY code, fiscal_year_end, submit_date DESC, doc_id DESC
)
"""
"""銘柄と期ごとに、いちばん新しい書類の数値だけを残したビュー.

訂正があればその数値になり、無ければ元の有報のままになる。分析はこちらを使う。
特定の書類を読みたいときは edinet_facts か edinet_statement_lines を直接見る。
"""


def upgrade() -> None:
    op.add_column(
        "edinet_documents",
        sa.Column(
            "fiscal_year_end",
            sa.Date(),
            nullable=True,
            comment="XBRL の DEI から読んだ会計年度末。解析するまで NULL。どの期の書類かを表す",
        ),
    )
    op.create_index(
        "ix_edinet_documents_code_fiscal_year_end",
        "edinet_documents",
        ["code", "fiscal_year_end"],
    )
    op.execute(_LATEST_FACTS_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS edinet_latest_facts")
    op.drop_index("ix_edinet_documents_code_fiscal_year_end", table_name="edinet_documents")
    op.drop_column("edinet_documents", "fiscal_year_end")
