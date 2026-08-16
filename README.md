# kabu-app

kabu の本番コード。取得バッチ、DB スキーマ、XBRL パーサ。**public**。

Mac で開発し、ラズパイが pull して実行する。バッチは cron ではなく systemd timer で回し、冪等に作る。

## データのパス

パスをコードに埋めない。実行環境で違うため `KABU_DATA_DIR` で受ける。

| 環境 | `KABU_DATA_DIR` |
| --- | --- |
| Mac | `~/workspace/kabu-terminal/data` (SMB, 読み取り専用) |
| ラズパイ | `/mnt/usb/data` |

## DB

PostgreSQL はラズパイのローカル (SSD 直) で動く。SMB 越しには置かない。

| ロール | 用途 | 権限 |
| --- | --- | --- |
| `kabu_dev` | Mac からの開発 | フル |
| `kabu_app` | ラズパイのバッチ (localhost のみ) | フル |
| `kabu_ro` | 分析・参照 | SELECT のみ |
