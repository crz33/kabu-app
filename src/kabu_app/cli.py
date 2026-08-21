"""kabu の CLI."""

import logging
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import httpx
import typer
from sqlalchemy.orm import Session

from kabu_app.collectors.edinet import (
    REQUEST_INTERVAL,
    document_path,
    download_document,
    fetch_document_list,
)
from kabu_app.collectors.jpx import fetch_stock_list, parse_stock_list
from kabu_app.collectors.tdnet import REQUEST_INTERVAL as TDNET_REQUEST_INTERVAL
from kabu_app.collectors.tdnet import (
    RETENTION_DAYS,
    disclosure_path,
    download_file,
    fetch_disclosure_list,
)
from kabu_app.collectors.yahoo import REQUEST_INTERVAL as YAHOO_REQUEST_INTERVAL
from kabu_app.collectors.yahoo import YahooPageError, fetch_quotes
from kabu_app.config import get_settings
from kabu_app.db import create_session_factory, session_scope
from kabu_app.stores.edinet import (
    latest_submit_date,
    load_documents,
    mark_downloaded,
    pending_documents,
)
from kabu_app.stores.stock import load_stock_list
from kabu_app.stores.tdnet import (
    count_expired,
    latest_disclosed_date,
    load_disclosures,
    pending_disclosures,
)
from kabu_app.stores.tdnet import mark_downloaded as mark_tdnet_downloaded
from kabu_app.stores.tick import (
    codes_with_price_jumps,
    earliest_dates,
    latest_prices,
    listed_codes,
    save_quotes,
)

logger = logging.getLogger("kabu_app")

_MAX_CONSECUTIVE_FAILURES = 10
"""ダウンロードがこの回数続けて失敗したら諦める。キー失効や障害で延々叩き続けないため."""

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
    # httpx はリクエストごとに URL を INFO で出す。日付を舐めるバッチでは量が多すぎる。
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)


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


@fetch_app.command("edinet")
def fetch_edinet(
    from_date: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="取得開始の提出日 (YYYY-MM-DD)。省略時は取り込み済みの最新提出日から",
        ),
    ] = None,
    to_date: Annotated[
        str | None,
        typer.Option("--to", help="取得終了の提出日 (YYYY-MM-DD)。省略時は今日"),
    ] = None,
    skip_download: Annotated[
        bool,
        typer.Option("--skip-download", help="メタデータだけ取り込み、ZIP は落とさない"),
    ] = False,
    max_download: Annotated[
        int | None,
        typer.Option("--max-download", help="1 回の実行で落とす ZIP の上限"),
    ] = None,
) -> None:
    """EDINET の有価証券報告書と訂正報告書を取得する.

    書類一覧は提出日単位でしか引けないため、期間を 1 日ずつ舐める。同じ日を 2 回処理しても
    doc_id で upsert するので壊れない。ZIP が未取得の書類は提出日にかかわらず毎回拾い直す。
    """
    settings = get_settings()
    api_key = settings.edinet_api_key
    if api_key is None:
        raise typer.BadParameter("EDINET_API_KEY が設定されていません。.env を確認してください")

    end = date.fromisoformat(to_date) if to_date else date.today()

    with session_scope(create_session_factory(settings.database_url)) as session:
        start = _resolve_start_date(session, from_date)
        if start > end:
            raise typer.BadParameter(f"開始日 {start} が終了日 {end} より後になっています")

        saved = _collect_metadata(session, start, end, api_key)

        if skip_download:
            logger.info(
                "完了: メタデータ %d 件 (--skip-download のため ZIP は取得していない)", saved
            )
            return

        downloaded, reused, failed = _download_pending(
            session, settings.kabu_data_dir, api_key, max_download
        )

    logger.info(
        "完了: %s 〜 %s / メタデータ %d 件 / ZIP 取得 %d 件 (既存 %d 件, 失敗 %d 件)",
        start,
        end,
        saved,
        downloaded,
        reused,
        failed,
    )


def _resolve_start_date(session: Session, from_date: str | None) -> date:
    """取得を始める提出日を決める.

    省略時は取り込み済みの最新提出日から。その日はもう一度引く。同じ日に後から提出された
    書類を取りこぼさないため。
    """
    if from_date is not None:
        return date.fromisoformat(from_date)

    latest = latest_submit_date(session)
    if latest is None:
        raise typer.BadParameter(
            "edinet_documents が空です。初回は --from で開始日を指定してください"
        )
    logger.info("取り込み済みの最新提出日 %s から再開する", latest)
    return latest


def _collect_metadata(session: Session, start: date, end: date, api_key: str) -> int:
    """期間を 1 日ずつ舐めて書類メタデータを取り込む."""
    logger.info("書類一覧を取得: %s 〜 %s (%d 日)", start, end, (end - start).days + 1)

    saved = 0
    target = start
    while target <= end:
        metas = fetch_document_list(target, api_key)
        count = load_documents(session, metas)
        session.commit()

        if count:
            logger.info("%s: %d 件", target, count)
        saved += count

        target += timedelta(days=1)
        if target <= end:
            time.sleep(REQUEST_INTERVAL)

    return saved


def _download_pending(
    session: Session, data_dir: Path, api_key: str, max_download: int | None
) -> tuple[int, int, int]:
    """ZIP が未取得の書類を落とす. 既にファイルがあれば記録だけ付ける."""
    pending = pending_documents(session, limit=max_download)
    if not pending:
        return 0, 0, 0

    logger.info("ZIP 未取得の書類が %d 件", len(pending))

    downloaded = 0
    reused = 0
    failed = 0
    consecutive_failures = 0

    for document in pending:
        path = document_path(data_dir, document.doc_id, document.submit_date)

        if path.exists():
            mark_downloaded(session, document.doc_id)
            session.commit()
            reused += 1
            continue

        try:
            download_document(document.doc_id, path, api_key)
        except (httpx.HTTPError, OSError) as error:
            failed += 1
            consecutive_failures += 1
            logger.warning("%s の取得に失敗: %s", document.doc_id, error)
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.error("%d 件続けて失敗したため中断する", consecutive_failures)
                break
            continue

        mark_downloaded(session, document.doc_id)
        session.commit()
        downloaded += 1
        consecutive_failures = 0

        if downloaded % 100 == 0:
            logger.info("ZIP 取得 %d / %d 件", downloaded + reused, len(pending))
        time.sleep(REQUEST_INTERVAL)

    return downloaded, reused, failed


@fetch_app.command("tdnet")
def fetch_tdnet(
    from_date: Annotated[
        str | None,
        typer.Option("--from", help="取得開始の開示日 (YYYY-MM-DD)。省略時は取り込み済みの最新日"),
    ] = None,
    to_date: Annotated[
        str | None,
        typer.Option("--to", help="取得終了の開示日 (YYYY-MM-DD)。省略時は今日"),
    ] = None,
    skip_download: Annotated[
        bool,
        typer.Option("--skip-download", help="メタデータだけ取り込み、実体は落とさない"),
    ] = False,
    max_download: Annotated[
        int | None,
        typer.Option("--max-download", help="1 回の実行で落とす実体の上限"),
    ] = None,
) -> None:
    """TDnet の決算短信とその訂正を取得する.

    一覧も実体ファイルも 31 日ほどで消える。取り逃した日は二度と取れないので、
    間を空けずに走らせること。EDINET のように後から遡ることはできない。
    """
    settings = get_settings()
    end = date.fromisoformat(to_date) if to_date else date.today()
    horizon = date.today() - timedelta(days=RETENTION_DAYS)

    with session_scope(create_session_factory(settings.database_url)) as session:
        start = _resolve_tdnet_start_date(session, from_date)
        if start > end:
            raise typer.BadParameter(f"開始日 {start} が終了日 {end} より後になっています")
        if start < horizon:
            logger.warning(
                "%s より前は TDnet から消えている。%s からの取得になる", horizon, horizon
            )
            start = horizon

        saved = _collect_disclosures(session, start, end)

        if skip_download:
            logger.info(
                "完了: メタデータ %d 件 (--skip-download のため実体は取得していない)", saved
            )
            return

        downloaded, reused, failed = _download_pending_disclosures(
            session, settings.kabu_data_dir, horizon, max_download
        )

        expired = count_expired(session, horizon)

    if expired:
        logger.warning("期限切れで取れなくなった開示が %d 件ある", expired)
    logger.info(
        "完了: %s 〜 %s / メタデータ %d 件 / ファイル %d 件取得 (既存 %d 件) / 失敗した開示 %d 件",
        start,
        end,
        saved,
        downloaded,
        reused,
        failed,
    )


def _resolve_tdnet_start_date(session: Session, from_date: str | None) -> date:
    """取得を始める開示日を決める. 省略時は取り込み済みの最新開示日から取り直す."""
    if from_date is not None:
        return date.fromisoformat(from_date)

    latest = latest_disclosed_date(session)
    if latest is None:
        raise typer.BadParameter(
            "tdnet_disclosures が空です。初回は --from で開始日を指定してください"
        )
    logger.info("取り込み済みの最新開示日 %s から再開する", latest)
    return latest


def _collect_disclosures(session: Session, start: date, end: date) -> int:
    """期間を 1 日ずつ舐めて開示メタデータを取り込む."""
    logger.info("開示一覧を取得: %s 〜 %s (%d 日)", start, end, (end - start).days + 1)

    saved = 0
    target = start
    while target <= end:
        metas = fetch_disclosure_list(target)
        count = load_disclosures(session, metas)
        session.commit()

        if count:
            logger.info("%s: %d 件", target, count)
        saved += count

        target += timedelta(days=1)
        if target <= end:
            time.sleep(TDNET_REQUEST_INTERVAL)

    return saved


def _download_pending_disclosures(
    session: Session, data_dir: Path, horizon: date, max_download: int | None
) -> tuple[int, int, int]:
    """実体が未取得の開示を落とす. PDF は必ず、XBRL はあるときだけ取る.

    XBRL の無い決算短信が 1 割ほどある。中間決算短信に目立つ。PDF を常に落とすのは、
    どの開示も同じ手順で読めるようにするため。ZIP しか無い開示があると、本文を読むのに
    2 系統の処理が要る。findocgen の既存データも PDF と ZIP の両方を持っている。
    """
    pending = pending_disclosures(session, horizon, limit=max_download)
    if not pending:
        return 0, 0, 0

    logger.info("実体が未取得の開示が %d 件", len(pending))

    downloaded = 0
    reused = 0
    failed = 0
    consecutive_failures = 0

    for index, disclosure in enumerate(pending, start=1):
        sources = [(f"{disclosure.doc_id}.pdf", "pdf")]
        if disclosure.xbrl_file is not None:
            sources.append((disclosure.xbrl_file, "zip"))

        completed = True
        for source, suffix in sources:
            path = disclosure_path(data_dir, disclosure.doc_id, disclosure.disclosed_date, suffix)
            if path.exists():
                reused += 1
                continue
            try:
                download_file(source, path)
            except (httpx.HTTPError, OSError) as error:
                logger.warning("%s の %s の取得に失敗: %s", disclosure.doc_id, suffix, error)
                completed = False
                break
            downloaded += 1
            time.sleep(TDNET_REQUEST_INTERVAL)

        if not completed:
            # 片方だけ落ちた開示は downloaded_at を埋めない。次の実行が残りを取りに行く。
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.error("%d 件続けて失敗したため中断する", consecutive_failures)
                break
            continue

        mark_tdnet_downloaded(session, disclosure.doc_id)
        session.commit()
        consecutive_failures = 0

        if index % 100 == 0:
            logger.info("開示 %d / %d 件", index, len(pending))

    return downloaded, reused, failed


@fetch_app.command("ticks")
def fetch_ticks(
    from_date: Annotated[
        str | None,
        typer.Option("--from", help="取得開始日 (YYYY-MM-DD)。省略時は銘柄ごとの最新取引日の翌日"),
    ] = None,
    to_date: Annotated[
        str | None,
        typer.Option("--to", help="取得終了日 (YYYY-MM-DD)。省略時は今日"),
    ] = None,
    codes: Annotated[
        str | None,
        typer.Option("--codes", help="銘柄コードをカンマ区切りで指定する。省略時は上場中の全銘柄"),
    ] = None,
    only_jumps: Annotated[
        bool,
        typer.Option("--only-jumps", help="終値が大きく飛んでいる銘柄だけを対象にする"),
    ] = False,
) -> None:
    """Yahoo Finance から日次の株価を取得する.

    1 ページ 20 営業日で、リクエストの間は 2 秒空ける。上場中の全銘柄を 1 年ぶん遡ると
    半日では終わらない。日々の更新は --from を省いて差分だけ取る。
    """
    settings = get_settings()
    end = date.fromisoformat(to_date) if to_date else date.today()

    with session_scope(create_session_factory(settings.database_url)) as session:
        targets = _resolve_tick_targets(session, codes, only_jumps)
        default_start = date.fromisoformat(from_date) if from_date is not None else None
        previous = {} if default_start is not None else latest_prices(session)

        logger.info("株価を取得: %d 銘柄 / 終了日 %s", len(targets), end)
        saved, failed, splits = _collect_ticks(session, targets, previous, default_start, end)

        if splits:
            saved += _refetch_split_codes(session, splits, end)

    logger.info("完了: %d 件保存 / 取得できなかった銘柄 %d 件", saved, failed)


def _resolve_tick_targets(session: Session, codes: str | None, only_jumps: bool) -> list[str]:
    """取得する銘柄を決める."""
    if codes is not None:
        return [code.strip() for code in codes.split(",") if code.strip()]
    if only_jumps:
        jumps = codes_with_price_jumps(session)
        logger.info("調整後終値が飛んでいる銘柄が %d 件", len(jumps))
        return jumps
    return listed_codes(session)


def _collect_ticks(
    session: Session,
    targets: list[str],
    previous: dict[str, tuple[date, Decimal]],
    default_start: date | None,
    end: date,
) -> tuple[int, int, list[str]]:
    """銘柄ごとに株価を取ってページ単位で書き込む.

    戻りは (保存件数, 取得できなかった銘柄数, 分割を検出した銘柄)。
    """
    saved = 0
    failed = 0
    splits: list[str] = []
    consecutive_failures = 0

    for index, code in enumerate(targets, start=1):
        watch = previous.get(code) if default_start is None else None
        start = default_start if default_start is not None else _overlap_start(watch)
        if start > end:
            continue

        try:
            count, split = _fetch_one_code(session, code, start, end, watch)
        except (YahooPageError, httpx.HTTPError) as error:
            failed += 1
            consecutive_failures += 1
            logger.warning("%s の取得に失敗: %s", code, error)
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.error("%d 件続けて失敗したため中断する", consecutive_failures)
                break
            continue

        saved += count
        consecutive_failures = 0
        if split:
            splits.append(code)
        if index % 100 == 0:
            logger.info("%d / %d 銘柄 (%d 件保存)", index, len(targets), saved)

    return saved, failed, splits


def _fetch_one_code(
    session: Session,
    code: str,
    start: date,
    end: date,
    watch: tuple[date, Decimal] | None,
) -> tuple[int, bool]:
    """1 銘柄を取り込む. 戻りは (保存件数, 分割を検出したか).

    watch は「取り込み済みの最新取引日とその日の調整後終値」。同じ日をもう一度取って値が
    変わっていれば、株式分割で過去まで書き換わったことになる。
    """
    saved = 0
    split = False

    for page in fetch_quotes(code, start, end):
        if watch is not None:
            watched_date, watched_price = watch
            split = split or any(
                quote.date == watched_date and quote.adjusted_close != watched_price
                for quote in page
            )
        saved += save_quotes(session, page)
        session.commit()
        # ページを取った直後に空ける。これが次の銘柄の 1 ページ目との間隔にもなる。
        time.sleep(YAHOO_REQUEST_INTERVAL)

    return saved, split


def _refetch_split_codes(session: Session, codes: list[str], end: date) -> int:
    """株式分割を検出した銘柄を、取り込み済みの最古日まで遡って取り直す.

    分割が起きると Yahoo は過去の調整後終値をすべて書き換える。差分だけ入れても、
    それより前の行は古い水準のまま残ってしまう。
    """
    logger.info("株式分割を検出したので取り直す: %s", ", ".join(codes))
    earliest = earliest_dates(session)

    saved = 0
    for code in codes:
        start = earliest.get(code)
        if start is None:
            continue
        try:
            count, _ = _fetch_one_code(session, code, start, end, None)
        except (YahooPageError, httpx.HTTPError) as error:
            logger.warning("%s の取り直しに失敗: %s", code, error)
            continue
        saved += count
        logger.info("%s を %s から取り直した (%d 件)", code, start, count)

    return saved


def _overlap_start(watch: tuple[date, Decimal] | None) -> date:
    """差分取得の開始日. 最新取引日そのものから取り、1 日ぶん重ねる.

    重ねるのは株式分割に気づくため。追加のリクエストは要らない。同じページに載っている。
    取り込みが無い銘柄は 1 年前から取る。
    """
    if watch is None:
        return date.today() - timedelta(days=365)
    return watch[0]
