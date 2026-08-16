#!/usr/bin/env bash
# kabu の DB とロールを作る。ラズパイで 1 度だけ実行する。
#
#   read -rs KABU_DEV_PASSWORD && export KABU_DEV_PASSWORD
#   read -rs KABU_APP_PASSWORD && export KABU_APP_PASSWORD
#   read -rs KABU_RO_PASSWORD  && export KABU_RO_PASSWORD
#   ./sql/bootstrap.sh
#
# パスワードは環境変数で受け、psql の標準入力に流す。
# コマンドライン引数には載せないため ps から見えない。
set -euo pipefail

sql_file="$(dirname "$0")/00_bootstrap.sql"

for var in KABU_DEV_PASSWORD KABU_APP_PASSWORD KABU_RO_PASSWORD; do
    if [ -z "${!var:-}" ]; then
        echo "環境変数 $var が未設定です" >&2
        exit 1
    fi
done

# psql の \set 用にエスケープする。バックスラッシュと単一引用符だけでよい。
# SQL リテラルとしてのクォートは :'変数名' の側が面倒を見る。
psql_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g"
}

{
    printf "\\set kabu_dev_password '%s'\n" "$(psql_escape "$KABU_DEV_PASSWORD")"
    printf "\\set kabu_app_password '%s'\n" "$(psql_escape "$KABU_APP_PASSWORD")"
    printf "\\set kabu_ro_password '%s'\n" "$(psql_escape "$KABU_RO_PASSWORD")"
    cat "$sql_file"
} | sudo -u postgres psql -v ON_ERROR_STOP=1 --no-psqlrc

echo "完了。次は Mac の .env に DATABASE_URL を設定して alembic upgrade head を流す。"
