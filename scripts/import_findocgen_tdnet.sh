#!/usr/bin/env bash
# findocgen の tdnet_docs を kabu の tdnet_disclosures に移す。
#
# 1 度だけ実行する。TDnet は 31 日より前を取り直せないため、2025-01-06 以降のメタデータは
# findocgen の DB にしかない。実体の ZIP と PDF は同じ配置に残っているので、downloaded_at を
# 埋めて取得済みにする。
#
#   FINDOCGEN_DATABASE_URL="$(grep '^DATABASE_URL=' ~/findocgen/.env | cut -d= -f2-)" \
#     ./scripts/import_findocgen_tdnet.sh
#
# 何度流しても増えない。doc_id が既にある行は飛ばす。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${FINDOCGEN_DATABASE_URL:?findocgen の DATABASE_URL を環境変数で渡してください}"

# cron ではなく手で叩くが、uv の場所は同じように通しておく。
export PATH="$HOME/.local/bin:$PATH"

# psql は SQLAlchemy 用の +psycopg を理解しない。
source_url="${FINDOCGEN_DATABASE_URL/postgresql+psycopg:\/\//postgresql://}"

dump="$(mktemp -t kabu-tdnet-import.XXXXXX)"
trap 'rm -f "$dump"' EXIT

# 書き込みは Python 側でやる。kabu_app には一時テーブルを作る権限が無いため。
psql "$source_url" --quiet -c "\copy (
    SELECT doc_id, submit_date, submit_time, code, filer_name, doc_desc, updated
    FROM tdnet_docs
    ORDER BY submit_date, submit_time
) TO STDOUT CSV" > "$dump"

uv run python scripts/import_findocgen_tdnet.py "$dump"
