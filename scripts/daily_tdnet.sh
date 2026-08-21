#!/usr/bin/env bash
# TDnet の決算短信と訂正短信を取得する。
#
# cron から日次で叩く。ログの行き先は呼び出し側で決める。
#
#   0 2 * * * /home/takada/kabu-app/scripts/daily_tdnet.sh 2>&1 | /usr/bin/logger -t kabu-tdnet
#
# 深夜 2 時に回して前日ぶんを確実に取る。開示は 23:55 まで出るため、当日中に走らせると
# 取りこぼす。取り込み済みの最新開示日から今日までを毎回舐め直すので、1 日 2 回走らせても
# 壊れない。
#
# EDINET と違い、一覧も実体も 31 日ほどで消える。取り逃した日は二度と取れない。
# 止めたまま 1 か月放置しないこと。
set -euo pipefail

# cron の PATH には ~/.local/bin が入っていない。uv を見つけるために足す。
export PATH="$HOME/.local/bin:$PATH"

# .env はカレントディレクトリから読まれる。リポジトリ直下に移ってから実行する。
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 多重起動を防ぐ。前の実行が残っていれば黙って抜ける。
# macOS には flock が無い。手元でのデバッグを通すため、無ければロックを諦める。
if command -v flock >/dev/null 2>&1; then
    exec 9>"/tmp/kabu-tdnet.lock"
    if ! flock -n 9; then
        echo "前の実行がまだ動いているため抜ける" >&2
        exit 0
    fi
fi

exec uv run kabu fetch tdnet
