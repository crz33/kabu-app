#!/usr/bin/env bash
# 毎晩の取得をまとめて回す。JPX → EDINET → TDnet → 株価 の順。
#
#   0 1 * * * /home/takada/kabu-app/scripts/nightly.sh 2>&1 | /usr/bin/logger -t kabu
#
# 01:00 に始める。TDnet の開示は 23:55 まで出るので日付が変わるまで待ち、Yahoo の 2 時間が
# 朝までに終わる時刻に置いてある。所要は 2〜3 時間。決算期の TDnet が重い日はもう少し伸びる。
#
# JPX を先頭に置くのは、新しく上場した銘柄を stocks に入れてから EDINET と株価を取るため。
# 月次更新のデータだが冪等で数秒なので、順序を保証するほうを取る。
#
# 途中で 1 つ落ちても後続は走らせる。TDnet は 31 日で消えるため、EDINET の失敗に
# 巻き込まれて止まるのが一番痛い。失敗があれば終了コードで返す。
set -uo pipefail

# cron の PATH には ~/.local/bin が入っていない。uv を見つけるために足す。
export PATH="$HOME/.local/bin:$PATH"

# .env はカレントディレクトリから読まれる。リポジトリ直下に移ってから実行する。
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 多重起動を防ぐ。前の晩の実行がまだ終わっていなければ黙って抜ける。
# macOS には flock が無い。手元でのデバッグを通すため、無ければロックを諦める。
if command -v flock >/dev/null 2>&1; then
    exec 9>"/tmp/kabu-nightly.lock"
    if ! flock -n 9; then
        echo "前の実行がまだ動いているため抜ける" >&2
        exit 0
    fi
fi

failed=()

run() {
    local label="$1"
    shift
    echo "=== ${label} 開始 ==="
    local started=$SECONDS
    if "$@"; then
        echo "=== ${label} 完了 ($((SECONDS - started)) 秒) ==="
    else
        echo "=== ${label} 失敗 (終了コード $?, $((SECONDS - started)) 秒) ===" >&2
        failed+=("${label}")
    fi
}

run "JPX 銘柄一覧" uv run kabu fetch jpx-stocks
run "EDINET"       uv run kabu fetch edinet
run "TDnet"        uv run kabu fetch tdnet
run "株価"          uv run kabu fetch ticks

if [ ${#failed[@]} -gt 0 ]; then
    echo "失敗した処理: ${failed[*]}" >&2
    exit 1
fi
echo "すべて完了 (${SECONDS} 秒)"
