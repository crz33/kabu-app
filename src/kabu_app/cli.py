"""kabu の CLI."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from kabu_app.collectors.jpx import fetch_stock_list, parse_stock_list
from kabu_app.config import get_settings
from kabu_app.db import create_session_factory, session_scope
from kabu_app.stores.stock import load_stock_list

logger = logging.getLogger("kabu_app")

app = typer.Typer(no_args_is_help=True, help="kabu の取得バッチ")
fetch_app = typer.Typer(no_args_is_help=True, help="外部データを取得して DB に入れる")
app.add_typer(fetch_app, name="fetch")


@app.callback()
def configure(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="DEBUG まで出す")] = False,
) -> None:
    """共通の初期化."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


@fetch_app.command("jpx-stocks")
def fetch_jpx_stocks(
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--from-file",
            help="ダウンロードせず既存の data_j.xls を読む。Mac でのデバッグ用",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """JPX の東証上場銘柄一覧を取得して stocks と stock_snapshots を更新する."""
    settings = get_settings()

    if from_file is None:
        _, data = fetch_stock_list(settings.kabu_data_dir)
    else:
        logger.info("ファイルから読む: %s", from_file)
        data = parse_stock_list(from_file)

    with session_scope(create_session_factory(settings.database_url)) as session:
        result = load_stock_list(session, data)

    logger.info(
        "完了: 基準日 %s / %d 銘柄 / 新規 %d / 上場廃止 %d / stocks 更新 %s",
        result.base_date,
        result.total,
        result.added,
        result.delisted,
        "あり" if result.stocks_updated else "なし",
    )
