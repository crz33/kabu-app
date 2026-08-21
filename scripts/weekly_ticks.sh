#!/usr/bin/env bash
# Yahoo Finance から日次の株価を取得する。
#
#   0 5 * * 6 /home/takada/kabu-app/scripts/weekly_ticks.sh 2>&1 | /usr/bin/logger -t kabu-ticks
#
# 土曜の 05:00 に週 1 回だけ回す。上場中の全銘柄を 2 秒間隔で叩くので 2 時間かかる。
# 日足は 1 日 1 本しか増えないため、毎晩やっても取れる量は変わらない。週 26000 リクエストと
# 週 3700 リクエストの差になるので、週次にしている。
#
# 銘柄ごとに最新取引日そのものから取り、1 日ぶん重ねる。重なった日の調整後終値が変わって
# いれば株式分割があったと分かり、その銘柄を最古日まで遡って取り直す。
set -euo pipefail

# cron の PATH には ~/.local/bin が入っていない。uv を見つけるために足す。
export PATH="$HOME/.local/bin:$PATH"

# .env はカレントディレクトリから読まれる。リポジトリ直下に移ってから実行する。
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 夜間バッチと同じロックを使う。DB と回線を共有するので同時に走らせる意味がない。
# こちらは待つ。週 1 回しか機会がないので、夜間バッチが長引いていても諦めたくない。
# macOS には flock が無い。手元でのデバッグを通すため、無ければロックを諦める。
if command -v flock >/dev/null 2>&1; then
    exec 9>"/tmp/kabu.lock"
    if ! flock -w 3600 9; then
        echo "1 時間待っても前の実行が終わらないため諦める" >&2
        exit 1
    fi
fi

exec uv run kabu fetch ticks
