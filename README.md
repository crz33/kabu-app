# kabu-app

kabu の本番コード。取得バッチ、DB スキーマ、XBRL パーサ。**public**。

Mac で開発し、ラズパイが pull して実行する。バッチは cron で回し、冪等に作る。

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
| `edinet_documents` | EDINET の有報・訂正有報のメタデータ。ZIP の取得状況も持つ。上場廃止した銘柄も入る |
| `tdnet_disclosures` | TDnet の決算短信・訂正短信のメタデータ。実体の取得状況も持つ |
| `ticks` | 日次の四本値と出来高。調整後終値も持つ。市場指数も同じ表に入る |

JPX の銘柄一覧 (`data_j.xls`) には `日付` 列があり、これが基準日になる。JPX は月末時点のデータを 1 か月ほど遅れて公開するため、取得日とは一致しない。`stock_snapshots.base_date` にはこの `日付` 列を使う。

## EDINET

有価証券報告書 (`120`) と訂正有価証券報告書 (`130`) を取得する。証券コードを持つ提出者
だけが対象になる。非上場の会社やファンドも有報を出すため、一覧の 1 割ほどしか残らない。

銘柄マスタとの突合はしない。上場廃止した会社の有報も入れる。`stocks` は JPX の最新一覧から
作るので、買収や MBO で消えた会社は載らない。ここで捨てると、過去を評価するときに生存者
バイアスが入る。2025 年の 10 か月で、有報を出した銘柄の 7% が今の一覧から消えていた。
上場中に絞りたい分析は `stocks` と結合する。

```bash
uv run kabu fetch edinet                    # 前回の続きから今日まで
uv run kabu fetch edinet --from 2025-01-06  # 開始日を指定する
uv run kabu fetch edinet --skip-download    # メタデータだけ入れる
uv run kabu fetch edinet --max-download 200 # ZIP の取得を 200 件で打ち切る
```

`--from` を省くと `edinet_documents` の最新提出日から取り直す。その日をもう一度引くのは、
同じ日に後から提出された書類を拾うため。書類は `doc_id` で upsert するので重複しない。

ZIP は `<KABU_DATA_DIR>/edinet/YYYYMMDD/{docID}.zip` に置く。既にファイルがあれば落とさず
記録だけ付ける。取得に失敗した書類は `downloaded_at` が NULL のまま残り、次の実行が提出日に
かかわらず拾い直す。取得済みの日を記録するテーブルは持たない。失敗した日を「済み」と
書いてしまう事故のほうが、空振りの一覧取得より高くつくため。

API キーは `EDINET_API_KEY` に入れる。キーが無効でも EDINET は HTTP 200 を返すので、本文の
`StatusCode` を見て落とす。見落とすと「対象 0 件」で静かに完走してしまう。

### 初回のバックフィル

前身の findocgen が 2025-01-06 以降の ZIP を `/mnt/usb/data/edinet/` に残している。同じ配置を
引き継ぐので、メタデータを入れ直せばファイルの再取得は起きない。

```bash
uv run kabu fetch edinet --from 2025-01-06
```

日数ぶんの一覧取得が走る。ZIP は既存ぶんを再利用し、findocgen が取っていなかった訂正有報
だけが新しく落ちる。

## TDnet

決算短信とその訂正を取得する。表題に「決算短信」を含む開示をすべて残す。「決算短信の
発表日変更のお知らせ」のような短信そのものでない開示も混じるが、捨てずに入れる。実体を
落とすかどうかは XBRL の有無で決める。

```bash
uv run kabu fetch tdnet                     # 前回の続きから今日まで
uv run kabu fetch tdnet --from 2026-08-15   # 開始日を指定する
uv run kabu fetch tdnet --skip-download     # メタデータだけ入れる
```

### 31 日で消える

EDINET と一番違うのはここ。一覧ページも実体ファイルも 31 日ほどで消える。**取り逃した日は
二度と取れない**。日次バッチを止めたまま 1 か月放置すると、その期間は永久に欠ける。

未取得のリトライも 31 日以内に限っている。それより古いものは何度叩いても 404 が返るだけ
なので、`pending_disclosures` が最初から対象にしない。取り逃した件数は実行の最後に警告で
出る。数が増えていたらバッチが止まっている。

### XBRL が無い短信がある

決算短信の 1 割ほどに XBRL が付かない。中間決算短信で目立つ。

そこで **PDF は必ず落とし、XBRL があるときだけ ZIP も落とす**。ZIP しか無い開示があると、
本文を読むのに 2 系統の処理が要る。同じ会社の短信を時系列で並べたときに読み方が変わるのが
一番厄介なので、どの開示も PDF で読めるようにしてある。findocgen の既存 2 万件も両方持って
いるので、そこと地続きになる。

数値は ZIP から取る。`XBRLData/Summary` に iXBRL、`XBRLData/Attachment/qualitative.htm` に
定性情報が入る。XBRL の無い 1 割は PDF から読むしかない。

実体は `<KABU_DATA_DIR>/tdnet/YYYYMMDD/{docID}.pdf` と `.zip` に置く。findocgen と同じ配置
なので、既存ぶんは落とし直さない。PDF は平均 471KB、年 9GB ほど増える。

### findocgen からのメタデータ移行

過去分は TDnet から取り直せない。ZIP は `/mnt/usb/data/tdnet/` に 25773 件残っているが、
メタデータは findocgen の PostgreSQL にしかない。1 度だけ移す。

```bash
FINDOCGEN_DATABASE_URL="$(grep '^DATABASE_URL=' ~/findocgen/.env | cut -d= -f2-)" \
  ./scripts/import_findocgen_tdnet.sh
```

移した行は `sec_code` `markets` `xbrl_file` が NULL になる。findocgen が持っていない列
だから。`downloaded_at` は埋まるので、実体を落とし直そうとはしない。

## 株価

Yahoo Finance の株価時系列ページから日次の四本値を取る。API は無いので HTML を読む。中身は
Next.js の RSC ペイロードに JSON で載っているため、DOM ではなくそちらを解く。

```bash
uv run kabu fetch ticks                          # 銘柄ごとに最新取引日の翌日から
uv run kabu fetch ticks --from 2024-01-04        # 全銘柄をその日から取り直す
uv run kabu fetch ticks --codes 7203,6758        # 銘柄を絞る
uv run kabu fetch ticks --only-jumps             # 終値が飛んでいる銘柄だけ
```

1 ページ 20 営業日。リクエストの間を 2 秒空ける。上場中の全銘柄で 2 時間かかるので、
バッチは週次にしてある。上場廃止すると Yahoo からページごと消えるため、対象は
`stocks.is_listed` が true の銘柄に限る。廃止前の株価は取り込み済みのぶんが残る。

### 調整後終値を必ず持つ

ページの 1 行は 8 つの値を持つ。始値・高値・安値・終値・出来高・**調整後終値**・PER・PBR。
PER と PBR は直近の日にしか入らないので使わない。

調整後終値は株式分割を遡って調整した値。これを持たないとバックテストが壊れる。前身の
findocgen は捨てていて、234 万行のうち **590 か所・561 銘柄**に「前日比 45% 以上の飛び」が
残っていた。全部が分割で、実際には起きていない暴落になる。分割するのは株価が上がった会社が
多いので、成績が体系的に甘くなる。

始値・高値・安値の調整値は提供されない。`adjusted_close / close` を掛けて揃える。

### 分割は自動で追いかける

分割が起きると Yahoo は過去の調整後終値をすべて書き換える。書き換わるのは**過去の行**なので、
翌日から差分を取っているだけでは永久に気づけない。

そこで差分取得は「最新取引日の翌日」ではなく「最新取引日そのもの」から取る。1 日ぶん重なる。
重なった日の調整後終値が DB と違えば分割があったと分かるので、その銘柄を取り込み済みの
最古日まで遡って取り直す。追加のリクエストは要らない。同じページに載っている。

保険として、調整後終値の飛びからも洗い出せる。閾値に頼るので取りこぼしの確認に使う。

```bash
uv run kabu fetch ticks --only-jumps --from 2024-01-04
```

見るのは `close` ではなく調整後の値。`close` は分割の日に必ず飛ぶが、それは正常な値である。

### findocgen からのデータ移行

234 万行あるので psql の COPY で流す。`ticks` が空のときだけ実行できる。

```bash
FINDOCGEN_DATABASE_URL="$(grep '^DATABASE_URL=' ~/findocgen/.env | cut -d= -f2-)" \
  ./scripts/import_findocgen_ticks.sh
```

移した行は `adjusted_close` が NULL になる。分割のあった 561 銘柄は上のコマンドで取り直す。
残りは分割していないので `close` をそのまま調整後とみなせる。

## バッチ

`scripts/` のシェルスクリプトを cron から叩く。スクリプトはリポジトリ直下に移動してから
実行するので、cron 側で `cd` は要らない。ログの行き先は cron 側で決める。

```cron
0 4 * * 0 /home/takada/kabu-app/scripts/weekly_jpx_stocks.sh 2>&1 | /usr/bin/logger -t kabu-jpx
0 22 * * * /home/takada/kabu-app/scripts/daily_edinet.sh      2>&1 | /usr/bin/logger -t kabu-edinet
0  2 * * * /home/takada/kabu-app/scripts/daily_tdnet.sh       2>&1 | /usr/bin/logger -t kabu-tdnet
```

```bash
journalctl -t kabu-jpx -n 50
journalctl -t kabu-edinet -n 50
journalctl -t kabu-tdnet -n 50
journalctl -t kabu-ticks -n 50
```

| スクリプト | 頻度 | 内容 |
| --- | --- | --- |
| `weekly_jpx_stocks.sh` | 毎週日曜 04:00 | JPX 銘柄一覧 |
| `daily_edinet.sh` | 毎日 22:00 | EDINET の有報・訂正有報 |
| `daily_tdnet.sh` | 毎日 02:00 | TDnet の決算短信・訂正短信 |
| `weekly_ticks.sh` | 毎週土曜 05:00 | Yahoo Finance の日次株価 |

JPX の一覧は月次更新のデータを週次で叩く。冪等なので、更新されていなければ DB も
生ファイルも変わらない。1 回失敗しても次の週に入るため、取りこぼしに気づかないまま
1 か月進むことがない。

systemd timer は使わない。ログは `logger` で journald に入り、多重起動は `flock` で
防げる。実行順の制御が要るジョブが増えたら考え直す。

## 開発

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src
uv run pytest
```
