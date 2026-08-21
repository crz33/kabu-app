#!/usr/bin/env bash
# Yahoo Finance から日次の株価を取得する。
#
# cron から週次で叩く。ログの行き先は呼び出し側で決める。
#
#   0 5 * * 6 /home/takada/kabu-app/scripts/weekly_ticks.sh 2>&1 | /usr/bin/logger -t kabu-ticks
#
# 上場中の全銘柄を回るので 2 時間ほどかかる。リクエストの間を 2 秒空けているため。
# 日次にすると毎日 2 時間走り続けることになるので週次にしている。
#
# 銘柄ごとに最新取引日の翌日から取る。前の週に失敗した銘柄も次の週にまとめて入る。
set -euo pipefail

# cron の PATH には ~/.local/bin が入っていない。uv を見つけるために足す。
export PATH="$HOME/.local/bin:$PATH"

# .env はカレントディレクトリから読まれる。リポジトリ直下に移ってから実行する。
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 多重起動を防ぐ。前の実行が残っていれば黙って抜ける。
# macOS には flock が無い。手元でのデバッグを通すため、無ければロックを諦める。
if command -v flock >/dev/null 2>&1; then
    exec 9>"/tmp/kabu-ticks.lock"
    if ! flock -n 9; then
        echo "前の実行がまだ動いているため抜ける" >&2
        exit 0
    fi
fi

exec uv run kabu fetch ticks
