"""有報と決算短信の財務項目を 1 つのビューにまとめる.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-23

同じ 6 項目が有報と短信の両方から取れる。引くたびにどちらを見るか考えないで済むよう
1 枚にまとめる。年次は有報、四半期は短信という分担になる。

ビューは 2 枚にする。``financials`` が報告のぜんぶ、``latest_financials`` が期ごとに 1 行。

**1 枚に絞ってはいけない**。有報は「主要な経営指標等」に 5 期分を載せるので、同じ期を何通も
の書類が報告する。期ごとに最新の書類だけを残すと、2019 年 3 月期の売上が「2025 年の有報で
報告された」形になり、``available_at`` が 2025 年になる。実際には 2019 年 6 月に分かっていた。
これで絞ると、2020 年時点の判定で 2019 年の売上が使えなくなる。

``available_at`` は「この値がこの書類で報告された日」を表す。有報の提出日と短信の開示日で、
**その数値がいつ使えるようになったか**になる。決算日から短信まで 45 日、有報まで 3 か月ある
ので、決算期末の日付で引くと存在しない情報を使うことになる。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINANCIALS_VIEW = """
CREATE VIEW financials AS
SELECT
    d.code,
    f.item,
    -- 有報は年次だけ。時点の項目 (総資産・純資産) は短信の year_end と揃える
    CASE WHEN f.period_start IS NULL THEN 'year_end' ELSE 'year' END AS period_kind,
    f.period_end,
    f.period_start,
    f.value,
    f.unit,
    'edinet' AS source,
    f.doc_id,
    d.submit_date AS available_at,
    f.source_section,
    f.source_concept
FROM edinet_financials f
JOIN edinet_documents d ON d.doc_id = f.doc_id
WHERE d.fiscal_year_end IS NOT NULL
UNION ALL
SELECT
    d.code,
    n.item,
    n.period_kind,
    n.period_end,
    n.period_start,
    n.value,
    n.unit,
    'tdnet' AS source,
    n.doc_id,
    d.disclosed_date AS available_at,
    n.source_section,
    n.source_concept
FROM tdnet_financials n
JOIN tdnet_disclosures d ON d.doc_id = n.doc_id
"""
"""報告のぜんぶを縦に並べたビュー. 同じ期が何行も出る.

ある時点で何が分かっていたかを再現するときはこちらを使う。

    SELECT DISTINCT ON (item) item, value
    FROM financials
    WHERE code = '7203' AND period_kind = 'year' AND available_at <= '2022-03-01'
    ORDER BY item, period_end DESC, available_at DESC;

``period_kind`` の値は短信に合わせてある。

- ``year`` / ``year_end``: 通期と期末時点。有報はここにしか入らない
- ``ytd``: 年初来累計。四半期短信の主軸になる
- ``quarter`` / ``quarter_end``: 単独四半期と四半期末時点
- ``interim`` / ``interim_end``: 中間期
"""

_LATEST_FINANCIALS_VIEW = """
CREATE VIEW latest_financials AS
SELECT DISTINCT ON (code, item, period_kind, period_end)
    code,
    item,
    period_kind,
    period_end,
    period_start,
    value,
    unit,
    source,
    doc_id,
    available_at,
    source_section,
    source_concept
FROM financials
ORDER BY
    code, item, period_kind, period_end,
    CASE source WHEN 'edinet' THEN 1 ELSE 2 END,
    available_at DESC,
    doc_id DESC
"""
"""銘柄・項目・期の種類・期末ごとに 1 行だけを残したビュー. 今の姿を見るときに使う.

同じ期を両方が報告していたら有報を採る。監査を通った確定値だからになる。ただし通期の短信が
出てから有報が出るまで 1.5 か月ほどあり、その間は短信しか無い。そこは短信を採る。

同じ出所で複数の書類が同じ期を報告することもある。訂正と、次の四半期に載る比較値が該当する。
そこは ``available_at`` の新しいほうを採る。

このビューで時点を再現しようとしないこと。``available_at`` は残った 1 行の出所の日付でしか
なく、有報の 5 期分をたどると古い期ほど新しい日付が付く。時点の再現には ``financials`` を使う。
"""


def upgrade() -> None:
    op.execute(_FINANCIALS_VIEW)
    op.execute(_LATEST_FINANCIALS_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS latest_financials")
    op.execute("DROP VIEW IF EXISTS financials")
