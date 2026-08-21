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
| `edinet_documents` | EDINET の有報・訂正有報のメタデータ。ZIP の取得状況も持つ |

JPX の銘柄一覧 (`data_j.xls`) には `日付` 列があり、これが基準日になる。JPX は月末時点のデータを 1 か月ほど遅れて公開するため、取得日とは一致しない。`stock_snapshots.base_date` にはこの `日付` 列を使う。

## EDINET

有価証券報告書 (`120`) と訂正有価証券報告書 (`130`) を取得する。証券コードを持つ提出者
だけが対象で、銘柄マスタに無いコードは捨てる。非上場の会社やファンドも有報を出すため、
一覧の 1 割ほどしか残らない。

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

## バッチ

`scripts/` のシェルスクリプトを cron から叩く。スクリプトはリポジトリ直下に移動してから
実行するので、cron 側で `cd` は要らない。ログの行き先は cron 側で決める。

```cron
0 4 * * 0 /home/takada/kabu-app/scripts/weekly_jpx_stocks.sh 2>&1 | /usr/bin/logger -t kabu-jpx
0 22 * * * /home/takada/kabu-app/scripts/daily_edinet.sh      2>&1 | /usr/bin/logger -t kabu-edinet
```

```bash
journalctl -t kabu-jpx -n 50
journalctl -t kabu-edinet -n 50
```

| スクリプト | 頻度 | 内容 |
| --- | --- | --- |
| `weekly_jpx_stocks.sh` | 毎週日曜 04:00 | JPX 銘柄一覧 |
| `daily_edinet.sh` | 毎日 22:00 | EDINET の有報・訂正有報 |

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
