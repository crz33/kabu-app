#!/usr/bin/env bash
# findocgen の ticks を kabu の ticks に移す。
#
# 1 度だけ実行する。234 万行あるので psql の COPY で流す。
#
#   FINDOCGEN_DATABASE_URL="$(grep '^DATABASE_URL=' ~/findocgen/.env | cut -d= -f2-)" \
#     ./scripts/import_findocgen_ticks.sh
#
# 移した行は adjusted_close が NULL になる。findocgen が持っていないため。株式分割のあった
# 銘柄はこのままでは分割前の水準で残るので、移したあとに取り直す。
#
#   uv run kabu fetch ticks --only-jumps --from 2024-01-04
#
# 空のときだけ流せる。重複を弾く仕組みを持たないため。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${FINDOCGEN_DATABASE_URL:?findocgen の DATABASE_URL を環境変数で渡してください}"

# psql は SQLAlchemy 用の +psycopg を理解しない。
source_url="${FINDOCGEN_DATABASE_URL/postgresql+psycopg:\/\//postgresql://}"
target_url="$(grep '^DATABASE_URL=' .env | cut -d= -f2-)"
target_url="${target_url/postgresql+psycopg:\/\//postgresql://}"

existing="$(psql "$target_url" -tAc 'SELECT count(*) FROM ticks')"
if [ "$existing" != "0" ]; then
    echo "ticks に既に ${existing} 行あります。空のときだけ流せます" >&2
    exit 1
fi

psql "$source_url" --quiet -c "\copy (
    SELECT code, date, open, high, low, close, volume
    FROM ticks
    ORDER BY code, date
) TO STDOUT CSV" \
  | psql "$target_url" --quiet --set ON_ERROR_STOP=1 \
      -c "\copy ticks (code, date, open, high, low, close, volume) FROM STDIN CSV"

psql "$target_url" -c "SELECT count(*) AS 行数, min(date) AS 最古, max(date) AS 最新,
    count(DISTINCT code) AS 銘柄数 FROM ticks"
