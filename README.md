# kabu-app

kabu の本番コード。取得バッチ、DB スキーマ、XBRL パーサ。**public**。

## 構成

2 台で動かす。手元の開発機でコードを書き、常時稼働する本番機が pull して cron で回す。
ここでは開発機が Mac、本番機がラズパイ + 直付けの SSD になる。以降もこの呼び方で書く。
バッチは冪等に作り、同じ日に 2 回走っても壊れないようにする。

kabu という投資判断システムの一部で、隣に 2 つある。`kabu-terminal` が作業ルート、
`kabu-lab` が分析側。どちらもこのリポジトリからは参照しない。

## 開発環境

Python 3.13 と [uv](https://docs.astral.sh/uv/) を使う。

```bash
git clone git@github.com:crz33/kabu-app.git
cd kabu-app
uv sync
cp .env.example .env   # 中身を埋める
```

`.env` は `.gitignore` 済み。パスワードを含むので `chmod 600` にしておく。

## データのパス

パスをコードに埋めない。実行環境で違うため `KABU_DATA_DIR` で受ける。

| 環境 | `KABU_DATA_DIR` |
| --- | --- |
| Mac (デバッグ) | `./.data` |
| ラズパイ | `/mnt/usb/data` |

`kabu-terminal/data` は SMB の読み取り専用マウントなので、ここには指定しない。書き込む処理を Mac で走らせないため。

## DB

PostgreSQL はラズパイのローカル (SSD 直) で動く。SMB 越しには置かない。ロールは 3 つ。

| ロール | 用途 | 権限 | 接続元 |
| --- | --- | --- | --- |
| `kabu_dev` | Mac からの開発。Alembic を流すのでテーブルの所有者 | フル | LAN |
| `kabu_app` | ラズパイのバッチ | フル | localhost のみ |
| `kabu_ro` | 分析・参照 (kabu-lab) | SELECT のみ | LAN |

`kabu_dev` が一番強い。テーブルの所有者なので、漏れると DROP まで通る。ラズパイの `.env` には置かない。

## ラズパイの初期構築

1 度だけ実行する。`sudo -u postgres` を使うのでラズパイ上で行う。

### 1. DB とロールを作る

パスワードは 3 つ生成して環境変数で渡す。`sql/bootstrap.sh` が psql の標準入力に流すため、コマンドライン引数にも `ps` にも載らない。

```bash
read -rs KABU_DEV_PASSWORD && export KABU_DEV_PASSWORD
read -rs KABU_APP_PASSWORD && export KABU_APP_PASSWORD
read -rs KABU_RO_PASSWORD  && export KABU_RO_PASSWORD
./sql/bootstrap.sh
```

`read -rs` は入力を表示せず、シェル履歴にも残さない。実行後はそのシェルを閉じる。テーブルはここでは作らない。Alembic の担当。

確認はこれで足りる。

```bash
sudo -u postgres psql -c '\du' -c '\l kabu'
sudo -u postgres psql -d kabu -c '\dn+' -c '\ddp'
```

### 2. 接続設定

Mac から `kabu_dev` で繋ぐため `postgresql.conf` を開ける。

```text
listen_addresses = '*'
```

`pg_hba.conf` は上から順に評価し、最初に一致した行で決まる。`kabu_app` を localhost のみに縛るには、LAN を許可する行より**前**に拒否行を置く。

```text
# TYPE  DATABASE  USER      ADDRESS            METHOD
host    all       kabu_app  192.168.0.0/24     reject
host    all       all       192.168.0.0/24     scram-sha-256
```

サブネットは自分の LAN に合わせる。`kabu_app` が localhost から繋ぐぶんは、既定の `127.0.0.1/32` と `::1/128` の行が拾う。`localhost` は環境によって IPv6 を先に引くので、両方必要。

```bash
sudo systemctl reload postgresql
sudo -u postgres psql -c "SELECT rule_number, type, database, user_name, address, auth_method FROM pg_hba_file_rules ORDER BY rule_number;"
```

`reject` の行が LAN 許可の行より小さい `rule_number` にあれば正しい。

### 3. `.env` を書く

パスワードは `DATABASE_URL` に埋める。`KABU_*_PASSWORD` を別に持たせない。同じ秘密が 2 つの形で入り、片方だけ書き換える事故が起きるため。

```text
# ラズパイ
DATABASE_URL=postgresql+psycopg://kabu_app:PASSWORD@localhost:5432/kabu
KABU_DATA_DIR=/mnt/usb/data
EDINET_API_KEY=<EDINET のサブスクリプションキー>

# Mac
DATABASE_URL=postgresql+psycopg://kabu_dev:PASSWORD@<ラズパイのIP>:5432/kabu
KABU_DATA_DIR=./.data
EDINET_API_KEY=<EDINET のサブスクリプションキー>
```

ラズパイの IP は DHCP だと変わる。ルータ側で固定するか、mDNS 名を使う。

EDINET のキーは https://api.edinet-fsa.go.jp/ で登録して発行する。Mac とラズパイで
同じキーを使ってよい。

## マイグレーション

Alembic で管理する。接続先は `alembic.ini` に書かず `DATABASE_URL` から取るので、Mac とラズパイで同じファイルが使える。

```bash
uv run alembic upgrade head          # 適用
uv run alembic check                 # モデルと DB が一致するか
uv run alembic revision --autogenerate -m "説明"
uv run alembic upgrade head --sql    # DB に触らず SQL を確認
```

制約とインデックスの名前は `src/kabu_app/models/base.py` の命名規則で固定している。DB 側の自動命名に任せると、autogenerate で名前がずれて差分が出続ける。

## テーブル

| テーブル | 内容 |
| --- | --- |
| `stocks` | 銘柄マスタ。最新状態のみ。上場廃止は削除せず `is_listed = false` |
| `stock_snapshots` | JPX 一覧の基準日ごとの全銘柄。市場変更や業種変更を後から追うため |
| `edinet_documents` | EDINET の有報・訂正有報のメタデータ。会計基準・連結の有無・ZIP と解析の状況も持つ |
| `edinet_facts` | 有報の財務諸表の数値。1 行 1 数値。連結全体は `member IS NULL` で絞る |
| `edinet_financials` | ファクトを共通の財務項目に名寄せした値。会社をまたいで並べられる |
| `edinet_labels` | 金融庁のタクソノミが定める要素名の標準ラベル |
| `edinet_document_labels` | 書類ごとのラベル。会社が付けた言い換えと独自の拡張要素 |
| `edinet_shareholders` | 有報「大株主の状況」の上位株主。種別とオーナー判定つき |
| `tdnet_disclosures` | TDnet の決算短信・訂正短信のメタデータ。実体と解析の状況も持つ |
| `tdnet_summary_facts` | 決算短信の表紙の数値。実績と**会社予想**。予想はここにしか無い |
| `tdnet_statement_facts` | 決算短信の添付にある財務諸表の数値。有報と同じ体系で四半期ごと |
| `tdnet_financials` | 添付を共通の財務項目に名寄せした値。有報と同じ 6 項目を四半期で |
| `ticks` | 日次の四本値と出来高。調整後終値も持つ。市場指数も同じ表に入る |

ビューが 5 つある。

| ビュー | 中身 |
| --- | --- |
| `edinet_statement_lines` | `edinet_facts` にラベルを結合したもの。`ORDER BY ordinal` で計算書の形になる |
| `edinet_latest_facts` | 銘柄と期ごとに、いちばん新しい書類のファクトだけを残したもの |
| `edinet_latest_financials` | 銘柄・項目・期ごとに 1 行だけを残した有報の財務項目 |
| `financials` | 有報と短信の財務項目を縦に並べたもの。同じ期が何行も出る |
| `latest_financials` | 上を銘柄・項目・期ごとに 1 行へ絞ったもの |

JPX の銘柄一覧 (`data_j.xls`) には `日付` 列があり、これが基準日になる。JPX は月末時点のデータを 1 か月ほど遅れて公開するため、取得日とは一致しない。`stock_snapshots.base_date` にはこの `日付` 列を使う。

列ごとの意味は書き写さない。すべての表と列に `COMMENT` が付いているので `psql` で読む。

```bash
psql "$DATABASE_URL" -c '\d+ edinet_financials'
```

## どれを引くか

テーブルの一覧より、こちらのほうが早く着く。

| 知りたいこと | 引き先 |
| --- | --- |
| 実績の財務 6 項目 | `latest_financials` |
| ある時点で分かっていた値 | `financials` を `available_at` で絞る |
| 会社予想 | `tdnet_summary_facts` の `fact_type = 'Forecast'` |
| 四半期の実績 | `tdnet_financials`。`period_kind` で累計と単独を選ぶ |
| 6 項目に無い勘定 | `edinet_latest_facts` / `tdnet_statement_facts` |
| セグメント別の値 | `edinet_facts` の `member` が非 NULL の行 |
| 計算書を刷られた形で読む | `edinet_statement_lines` |
| 株価・出来高 | `ticks`。長期の比較は `adjusted_close` |
| 大株主・オーナー比率 | `edinet_shareholders` |

名寄せの 6 項目は売上高・営業利益・経常利益・当期純利益・総資産・純資産。定義は
`src/kabu_app/normalizers/financials.py` の `ITEM_SPECS` にある。

実績を `tdnet_summary_facts` から引かないこと。表紙は百万円に丸めてある。表紙を読むのは
会社予想のためになる。

### どのコマンドが埋めるか

| コマンド | 埋まるテーブル |
| --- | --- |
| `kabu fetch jpx-stocks` | `stocks` / `stock_snapshots` |
| `kabu fetch edinet` | `edinet_documents` (メタと ZIP の状況) |
| `kabu parse edinet` | `edinet_facts` / `edinet_document_labels` / `edinet_shareholders` |
| `kabu normalize financials` | `edinet_financials` |
| `kabu fetch tdnet` | `tdnet_disclosures` (メタと実体の状況) |
| `kabu parse tdnet` | `tdnet_summary_facts` / `tdnet_statement_facts` |
| `kabu normalize tdnet-financials` | `tdnet_financials` |
| `kabu fetch ticks` | `ticks`。`--only-jumps` のときだけ `tick_jump_checks` も |
| `kabu parse taxonomy YYYY` | `edinet_labels` |

`fetch` と `parse` は同じメタデータ表を 2 段階で埋める。`parse` が
`accounting_standard` `is_consolidated` `fiscal_year_end` `parsed_at` を後から入れる。
`downloaded_at` や `parsed_at` が NULL の行は未処理で、次の実行が拾い直す。

`kabu parse taxonomy` だけはバッチに入っていない。ZIP を手で置いてから流す。

### 生ファイルの置き場

`KABU_DATA_DIR` の下に取得元ごとに分ける。

| パス | 中身 |
| --- | --- |
| `jpx/stock_list/data_j_YYYYMMDD.xlsx` | JPX 東証上場銘柄一覧 |
| `edinet/YYYYMMDD/{docID}.zip` | 有報の XBRL 一式。ディレクトリは提出日 |
| `tdnet/YYYYMMDD/{docID}.zip` | 短信の XBRL 一式。ディレクトリは開示日 |
| `tdnet/YYYYMMDD/{docID}.pdf` | XBRL が無い開示の本体 |
| `edinet_taxonomy/Taxonomy_YYYY.zip` | 金融庁のタクソノミ。手で置く |

## 使い方

取得と解析と名寄せを分けてある。どれも冪等で、途中で止めても同じコマンドを叩き直せば残り
だけを処理する。

```bash
uv run kabu fetch jpx-stocks                    # 銘柄一覧。月次更新だが毎晩流してよい
uv run kabu fetch edinet                        # 前回の続きから今日まで
uv run kabu fetch tdnet                         # 同上。31 日より前は取れない
uv run kabu fetch ticks                         # 銘柄ごとに最新取引日から。2 時間かかる
uv run kabu fetch ticks --only-jumps            # 調整後終値が飛んでいる銘柄だけ取り直す
uv run kabu parse edinet                        # 未解析の有報を解析する
uv run kabu parse tdnet                         # 未解析の短信を解析する
uv run kabu parse taxonomy 2026                 # タクソノミの標準ラベルを入れる
uv run kabu normalize financials                # 有報のファクトを 6 項目に寄せる
uv run kabu normalize tdnet-financials          # 短信の添付を同じ 6 項目に寄せる
```

主な option は 3 系統ある。`--help` に全部載っている。

| option | 効き方 |
| --- | --- |
| `--from` / `--to` | 対象の日付を指定する。省くと取り込み済みの続きから今日まで |
| `--max-download` / `--max-documents` / `--max-codes` | 1 回の実行で扱う数を打ち切る |
| `--reparse` / `--renormalize` / `--doc-id` | 済みのものをやり直す。パーサや項目の定義を直したとき |

失敗した書類は `downloaded_at` や `parsed_at` が NULL のまま残り、次の実行が拾い直す。理由は
`parse_error` に入る。取得済みの日を記録するテーブルは持たない。失敗した日を「済み」と書いて
しまう事故のほうが、空振りの一覧取得より高くつくため。

項目の定義を変えたときはバッチ任せにせず、手で `--renormalize` を流すこと。ファイルは読まず
ファクトから作り直すので、有報の全件で 9 分で済む。

## 設計の理由はコードにある

なぜこの形なのかは、判断した場所の docstring に書いてある。ここには書き写さない。2 か所に
置くと必ず片方が古くなる。

| 知りたいこと | 読む先 |
| --- | --- |
| 有報の XBRL をどう読むか | `parsers/edinet_xbrl.py` |
| 表示順と階層の作り方 | `parsers/edinet_xbrl.py` の `_build_lines` まわり |
| 大株主のオーナー判定 | `parsers/shareholders.py` |
| タクソノミのラベル | `parsers/taxonomy.py` と `models/edinet.py` の `EdinetLabel` |
| 短信の iXBRL をどう読むか | `parsers/tdnet_xbrl.py` |
| 6 項目への名寄せ | `normalizers/financials.py` |
| 短信の期の数え方 | `normalizers/tdnet_financials.py` |
| 連結と単体の切り替え | `stores/edinet_financial.py` と `stores/tdnet_fact.py` |
| 各表と列の意味 | `models/` と DB の `COMMENT` |
| ビューの選び方と `available_at` | ビューを作った migration |
| 訂正有報を取り込む判断 | `migrations/versions/20260823_0008_*.py` |
| 株価の取得と締め出し | `collectors/yahoo.py` |
| TDnet が 31 日で消える扱い | `collectors/tdnet.py` |
| バッチの順序とロック | `scripts/nightly.sh` と `scripts/weekly_ticks.sh` |

## 遡れる範囲

いま入っているデータには始点がある。分析に効くのでここに書く。

- **EDINET の取得は 2025-01-06 から。** それ以前に提出された書類は入っていない。2024 年より
  前の期のデータは「2025 年以降の書類が報告した過去の期」として入っており、その期の当時の
  報告そのものではない
- **有報は「主要な経営指標等」に 5 期分を載せる。** これが過去に伸ばす唯一の手になる。
  EDINET API は直近 5 年しか引けない。営業利益だけは載らないので当期と前期の 2 期になる
- **TDnet は取り逃すと二度と取れない。** 一覧も実体も 31 日で消える。バッチを 1 か月止めると
  その期間は永久に欠ける
- **株価は findocgen から引き継いだぶんの `adjusted_close` が NULL。** 分割のあった銘柄は
  夜間バッチが 1 晩 50 銘柄ずつ取り直している

## findocgen からの移行

前身の findocgen から株価とメタデータを引き継いだ。どれも 1 度だけ流す。

```bash
# 株価 234 万行。ticks が空のときだけ
FINDOCGEN_DATABASE_URL="$(grep '^DATABASE_URL=' ~/findocgen/.env | cut -d= -f2-)" \
  ./scripts/import_findocgen_ticks.sh

# 短信のメタデータ。実体は置き場にあるが、メタは findocgen にしかない
FINDOCGEN_DATABASE_URL="$(grep '^DATABASE_URL=' ~/findocgen/.env | cut -d= -f2-)" \
  ./scripts/import_findocgen_tdnet.sh

# 有報は ZIP を引き継ぐだけ。メタデータを入れ直せば再取得は起きない
uv run kabu fetch edinet --from 2025-01-06
```

移した行は findocgen が持っていなかった列が NULL になる。`ticks` は `adjusted_close`、
`tdnet_disclosures` は `sec_code` `markets` `xbrl_file`。株価は移したあとに取り直す。

```bash
uv run kabu fetch ticks --only-jumps --from 2024-01-04
```

## バッチ

cron から 2 本。スクリプトはリポジトリ直下に移動してから実行するので、cron 側で `cd` は
要らない。順序と待ち方の理由はスクリプトの先頭コメントにある。

`$HOME` はそのまま書ける。cron は `/etc/passwd` から `HOME` を入れ、コマンドを `/bin/sh`
に通すため。リポジトリを別の場所に置いたときはここを直す。

```cron
0 1 * * *  $HOME/kabu-app/scripts/nightly.sh      2>&1 | /usr/bin/logger -t kabu
0 3 * * 6  $HOME/kabu-app/scripts/weekly_ticks.sh 2>&1 | /usr/bin/logger -t kabu-ticks
```

```bash
journalctl -t kabu -n 100
journalctl -t kabu-ticks -n 50
```

`nightly.sh` は 8 つを順に回す。所要は 20〜105 分。

| 順 | 処理 | 所要 |
| --- | --- | --- |
| 1 | JPX 銘柄一覧 | 数秒 |
| 2 | EDINET | 1〜5 分 |
| 3 | EDINET 解析 | 1〜5 分 |
| 4 | 財務項目の名寄せ | 数秒 |
| 5 | TDnet | 5〜30 分 (決算期のピークで) |
| 6 | TDnet 解析 | 1〜10 分 (決算期のピークで) |
| 7 | 短信の名寄せ | 数秒 |
| 8 | 株価の飛び直し (50 銘柄) | 65 分 |

`weekly_ticks.sh` は株価の取得だけ。上場中の全銘柄を 2 秒間隔で叩くので 2 時間 20 分かかる。
03:00 なのは `apt-daily-upgrade.timer` の窓 (06:00〜07:00) を避けるためで、理由はスクリプトの
先頭に書いてある。

両方とも `/tmp/kabu.lock` を共有する。DB と回線を分け合うので同時に走らせない。

## 開発

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src
uv run pytest
```
