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

# Mac
DATABASE_URL=postgresql+psycopg://kabu_dev:PASSWORD@<ラズパイのIP>:5432/kabu
KABU_DATA_DIR=./.data
```

ラズパイの IP は DHCP だと変わる。ルータ側で固定するか、mDNS 名を使う。

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

JPX の銘柄一覧 (`data_j.xls`) には `日付` 列があり、これが基準日になる。JPX は月末時点のデータを 1 か月ほど遅れて公開するため、取得日とは一致しない。`stock_snapshots.base_date` にはこの `日付` 列を使う。

## バッチ

`scripts/` のシェルスクリプトを cron から叩く。スクリプトはリポジトリ直下に移動してから
実行するので、cron 側で `cd` は要らない。ログの行き先は cron 側で決める。

```cron
0 4 * * 0 /home/pi/kabu-app/scripts/weekly_jpx_stocks.sh 2>&1 | /usr/bin/logger -t kabu-jpx
```

```bash
journalctl -t kabu-jpx -n 50
```

| スクリプト | 頻度 | 内容 |
| --- | --- | --- |
| `weekly_jpx_stocks.sh` | 毎週日曜 04:00 | JPX 銘柄一覧 |

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
