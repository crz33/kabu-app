# kabu-app

kabu の本番コード。取得バッチ、DB スキーマ、XBRL パーサ。ラズパイが pull して実行する。

## 何をここに書くか

判断は 1 本。**読むのはいつか。**

データを作るときに読むなら、ここに書く。銘柄や市況を判断するときに読むなら `../kessannote/reports/` に書く。

「EDINET API は直近 5 年分しか遡れない」はバッチを直すときにしか読まないので、ここになる。「この期間の適時開示は網羅していないので集計に使わない」は判断のときに読むので kessannote になる。

理由は判断した場所の docstring に書く。README には書き写さない。2 か所に置くと必ず片方が古くなる。README は入口に絞ってある。

kessannote からこのリポジトリの実装ファイルへリンクは張らない。リファクタで嘘になる。

## 実装の約束

- データのパスをコードに埋めない。`KABU_DATA_DIR` で受ける (Mac は `kabu-app/.data`、ラズパイは `/mnt/usb/data`)
- `kabu-terminal/data` は SMB の読み取り専用マウント。`KABU_DATA_DIR` に指定しない。参照だけに使う
- バッチは `scripts/` のシェルスクリプトを cron から叩く。冪等に作り、同じ日に 2 回走っても壊れないこと
- PostgreSQL への接続はロールを使い分ける。バッチは `kabu_app` (localhost のみ)、開発は `kabu_dev`
