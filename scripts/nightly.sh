#!/usr/bin/env bash
# 毎晩の取得をまとめて回す。JPX → EDINET → EDINET 解析 → TDnet の順。
#
#   0 1 * * * /home/takada/kabu-app/scripts/nightly.sh 2>&1 | /usr/bin/logger -t kabu
#
# 01:00 に始める。TDnet の開示は 23:55 まで出るので、日付が変わるまで待たないと当日ぶんを
# 取りこぼす。所要は 10〜40 分。決算期の TDnet が重い日はもう少し伸びる。
#
# 株価の差分取得はここに入れない。週次の weekly_ticks.sh が担当する。日足は 1 日 1 本しか
# 増えないのに、毎晩やると Yahoo に週 26000 リクエスト投げることになるため。
#
# 入れてあるのは株式分割で調整が狂った銘柄の遡り。1 晩 50 銘柄に絞る。遡りは 1 銘柄で
# 40 ページ近く叩くので、まとめて流すと締められる。対象が尽きれば 0 件で即座に終わる。
#
# JPX を先頭に置くのは、新しく上場した銘柄を stocks に入れてから EDINET を取るため。
# 月次更新のデータだが冪等で数秒なので、順序を保証するほうを取る。
#
# 途中で 1 つ落ちても後続は走らせる。TDnet は 31 日で消えるため、EDINET の失敗に
# 巻き込まれて止まるのが一番痛い。失敗があれば終了コードで返す。
set -uo pipefail

# cron の PATH には ~/.local/bin が入っていない。uv を見つけるために足す。
export PATH="$HOME/.local/bin:$PATH"

# .env はカレントディレクトリから読まれる。リポジトリ直下に移ってから実行する。
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# 週次の株価取得と同じロックを使う。DB と回線を共有するので同時に走らせる意味がない。
# 前の実行がまだ終わっていなければ黙って抜ける。次の晩に入り直せばよい。
# macOS には flock が無い。手元でのデバッグを通すため、無ければロックを諦める。
if command -v flock >/dev/null 2>&1; then
    exec 9>"/tmp/kabu.lock"
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

run "JPX 銘柄一覧"   uv run kabu fetch jpx-stocks
run "EDINET"        uv run kabu fetch edinet
run "EDINET 解析"    uv run kabu parse edinet
run "TDnet"         uv run kabu fetch tdnet
run "株価の遡り"     uv run kabu fetch ticks --only-jumps --from 2024-01-04 --max-codes 50

if [ ${#failed[@]} -gt 0 ]; then
    echo "失敗した処理: ${failed[*]}" >&2
    exit 1
fi
echo "すべて完了 (${SECONDS} 秒)"
