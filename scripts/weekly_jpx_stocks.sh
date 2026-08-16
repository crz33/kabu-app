#!/usr/bin/env bash
# JPX 銘柄一覧を取得して stocks と stock_snapshots を更新する。
#
# cron から週次で叩く。ログの行き先は呼び出し側で決める。
#
#   0 4 * * 0 /home/pi/kabu-app/scripts/weekly_jpx_stocks.sh 2>&1 | /usr/bin/logger -t kabu-jpx
#
# 冪等なので、JPX が更新していなければ DB も生ファイルも変わらない。月次更新の
# データを週次で叩くのは、1 回失敗しても次の週に入るようにするため。
set -euo pipefail

# cron の PATH には ~/.local/bin が入っていない。uv を見つけるために足す。
export PATH="$HOME/.local/bin:$PATH"

# .env はカレントディレクトリから読まれる。リポジトリ直下に移ってから実行する。
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 多重起動を防ぐ。前の実行が残っていれば黙って抜ける。
# macOS には flock が無い。手元でのデバッグを通すため、無ければロックを諦める。
if command -v flock >/dev/null 2>&1; then
    exec 9>"/tmp/kabu-jpx-stocks.lock"
    if ! flock -n 9; then
        echo "前の実行がまだ動いているため抜ける" >&2
        exit 0
    fi
fi

exec uv run kabu fetch jpx-stocks
