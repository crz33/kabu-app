#!/usr/bin/env bash
# findocgen の tdnet_docs を kabu の tdnet_disclosures に移す。
#
# 1 度だけ実行する。TDnet は 31 日より前を取り直せないため、2025-01-06 以降のメタデータは
# findocgen の DB にしかない。実体の ZIP は同じ配置に残っているので、downloaded_at を
# 埋めて取得済みにする。
#
#   FINDOCGEN_DATABASE_URL="$(grep '^DATABASE_URL=' ~/findocgen/.env | cut -d= -f2-)" \
#     ./scripts/import_findocgen_tdnet.sh
#
# 何度流しても増えない。doc_id が既にある行は飛ばす。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${FINDOCGEN_DATABASE_URL:?findocgen の DATABASE_URL を環境変数で渡してください}"

# psql は SQLAlchemy 用の +psycopg を理解しない。
to_psql_url() {
    echo "${1/postgresql+psycopg:\/\//postgresql://}"
}

source_url="$(to_psql_url "$FINDOCGEN_DATABASE_URL")"
target_url="$(to_psql_url "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)")"

dump="$(mktemp -t kabu-tdnet-import.XXXXXX.csv)"
trap 'rm -f "$dump"' EXIT

psql "$source_url" --quiet -c \
    "\copy (SELECT doc_id, submit_date, submit_time, code, filer_name, doc_desc, updated FROM tdnet_docs ORDER BY submit_date, submit_time) TO STDOUT CSV" \
    > "$dump"
echo "findocgen から $(wc -l < "$dump") 行を読んだ"

psql "$target_url" --quiet --set ON_ERROR_STOP=1 <<SQL
CREATE TEMP TABLE staging (
    doc_id text,
    submit_date date,
    submit_time time,
    code text,
    filer_name text,
    doc_desc text,
    updated timestamptz
);

\copy staging FROM '$dump' CSV

INSERT INTO tdnet_disclosures (
    doc_id, disclosed_date, disclosed_time, sec_code, code,
    company_name, title, markets, is_amendment, xbrl_file, downloaded_at
)
SELECT
    doc_id, submit_date, submit_time, NULL, code,
    filer_name, doc_desc, NULL, doc_desc LIKE '%訂正%', NULL, updated
FROM staging
ON CONFLICT (doc_id) DO NOTHING;

SELECT count(*) AS "取り込み後の件数", min(disclosed_date) AS "最古", max(disclosed_date) AS "最新"
FROM tdnet_disclosures;
SQL
