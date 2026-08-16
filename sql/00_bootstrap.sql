-- kabu の DB とロールを作る。ラズパイで 1 度だけ実行する。
-- 直接叩かず sql/bootstrap.sh 経由で実行すること。パスワードは psql 変数で受ける。
--
-- テーブルは Alembic が作るのでここでは作らない。

\if :{?kabu_dev_password}
\else
\echo 'ERROR: パスワードが渡されていません。sql/bootstrap.sh から実行してください。'
\quit
\endif

-- ロール ---------------------------------------------------------------------
-- kabu_dev : Mac からの開発。Alembic を流すのでテーブルの所有者になる
-- kabu_app : ラズパイのバッチ。localhost からのみ接続させる (pg_hba.conf で制御)
-- kabu_ro  : 分析・参照。SELECT のみ
CREATE ROLE kabu_dev LOGIN PASSWORD :'kabu_dev_password';
CREATE ROLE kabu_app LOGIN PASSWORD :'kabu_app_password';
CREATE ROLE kabu_ro  LOGIN PASSWORD :'kabu_ro_password';

-- データベース ---------------------------------------------------------------
-- 照合順序は C。索引が速く、環境によるソート順のぶれがない。
-- 銘柄名を日本語順に並べたくなったら ORDER BY 側で COLLATE を指定する。
CREATE DATABASE kabu
    OWNER       kabu_dev
    ENCODING    'UTF8'
    LC_COLLATE  'C'
    LC_CTYPE    'C'
    TEMPLATE    template0;

\connect kabu

-- 接続権限 -------------------------------------------------------------------
-- ALL を落とす。CONNECT だけ revoke すると TEMPORARY が残る。
REVOKE ALL     ON DATABASE kabu FROM PUBLIC;
GRANT  CONNECT ON DATABASE kabu TO kabu_dev, kabu_app, kabu_ro;

-- スキーマ -------------------------------------------------------------------
ALTER SCHEMA public OWNER TO kabu_dev;
REVOKE ALL   ON SCHEMA public FROM PUBLIC;
GRANT  USAGE ON SCHEMA public TO kabu_app, kabu_ro;

-- 既存オブジェクトへの権限 ---------------------------------------------------
-- 初回は空。Alembic を流し直したあとに再実行しても害はない。
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO kabu_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO kabu_app;
GRANT SELECT                         ON ALL TABLES    IN SCHEMA public TO kabu_ro;

-- これから kabu_dev が作るオブジェクトへの権限 -------------------------------
-- Alembic は kabu_dev で流すため、新しいテーブルにも自動で権限が付く。
ALTER DEFAULT PRIVILEGES FOR ROLE kabu_dev IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kabu_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kabu_dev IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO kabu_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kabu_dev IN SCHEMA public
    GRANT SELECT ON TABLES TO kabu_ro;
