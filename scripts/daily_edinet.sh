#!/usr/bin/env bash
# EDINET の有価証券報告書と訂正有価証券報告書を取得する。
#
# cron から日次で叩く。ログの行き先は呼び出し側で決める。
#
#   0 22 * * * /home/takada/kabu-app/scripts/daily_edinet.sh 2>&1 | /usr/bin/logger -t kabu-edinet
#
# 取り込み済みの最新提出日から今日までを毎回舐め直す。書類は doc_id で upsert し、
# ZIP は既にあれば落とさないので、何度走っても壊れない。前回落とし損ねた ZIP も
# 次の実行が拾い直す。有報が出ない日が 2 週間続くこともあるが、空振りの一覧取得が
# その日数ぶん増えるだけで済む。
set -euo pipefail

# cron の PATH には ~/.local/bin が入っていない。uv を見つけるために足す。
export PATH="$HOME/.local/bin:$PATH"

# .env はカレントディレクトリから読まれる。リポジトリ直下に移ってから実行する。
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 多重起動を防ぐ。前の実行が残っていれば黙って抜ける。
# macOS には flock が無い。手元でのデバッグを通すため、無ければロックを諦める。
if command -v flock >/dev/null 2>&1; then
    exec 9>"/tmp/kabu-edinet.lock"
    if ! flock -n 9; then
        echo "前の実行がまだ動いているため抜ける" >&2
        exit 0
    fi
fi

exec uv run kabu fetch edinet
