"""kabu の CLI."""

import logging
import time
import zipfile
from collections.abc import Sequence
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
from kabu_app.collectors.yahoo import YahooPageError, create_client, fetch_quotes, iter_periods
from kabu_app.config import get_settings
from kabu_app.db import create_session_factory, session_scope
from kabu_app.models import EdinetDocument, TdnetDisclosure
from kabu_app.normalizers.financials import ITEMS, normalize
from kabu_app.normalizers.tdnet_financials import normalize as normalize_tdnet
from kabu_app.normalizers.tdnet_financials import normalize_summary as normalize_tdnet_summary
from kabu_app.parsers import EdinetXbrlError, parse_document, parse_shareholders
from kabu_app.parsers.taxonomy import parse_taxonomy_labels, taxonomy_path
from kabu_app.parsers.tdnet_xbrl import TdnetXbrlError, complete_info, parse_disclosure
from kabu_app.stores.edinet import (
    latest_submit_date,
    load_documents,
    mark_downloaded,
    pending_documents,
)
from kabu_app.stores.edinet_fact import (
    documents_by_id,
    mark_parsed,
    save_document_labels,
    save_facts,
    save_labels,
    save_shareholders,
    unparsed_documents,
)
from kabu_app.stores.edinet_financial import (
    documents_to_normalize,
    load_source_facts,
    save_financials,
)
from kabu_app.stores.stock import load_stock_list
from kabu_app.stores.tdnet import (
    count_expired,
    latest_disclosed_date,
    load_disclosures,
    pending_disclosures,
)
from kabu_app.stores.tdnet import mark_downloaded as mark_tdnet_downloaded
from kabu_app.stores.tdnet_fact import (
    disclosures_by_id,
    disclosures_to_normalize,
    load_statement_source_facts,
    load_summary_source_facts,
    mark_no_xbrl,
    save_statement_facts,
    save_summary_facts,
    save_tdnet_financials,
    unparsed_disclosures,
)
from kabu_app.stores.tdnet_fact import mark_parsed as mark_tdnet_parsed
from kabu_app.stores.tick import (
    codes_missing_adjusted,
    codes_with_price_jumps,
    earliest_dates,
    latest_prices,
    listed_codes,
    price_jumps,
    save_jump_checks,
    save_quotes,
)

logger = logging.getLogger("kabu_app")

_MAX_CONSECUTIVE_FAILURES = 10
"""ダウンロードがこの回数続けて失敗したら諦める。キー失効や障害で延々叩き続けないため."""

app = typer.Typer(no_args_is_help=True, help="kabu の取得バッチ")
fetch_app = typer.Typer(no_args_is_help=True, help="外部データを取得して DB に入れる")
app.add_typer(fetch_app, name="fetch")
parse_app = typer.Typer(no_args_is_help=True, help="取得済みのファイルを解析して DB に入れる")
app.add_typer(parse_app, name="parse")
normalize_app = typer.Typer(no_args_is_help=True, help="取り込み済みのデータを名寄せする")
app.add_typer(normalize_app, name="normalize")


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
        typer.Option("--only-jumps", help="調整後終値が大きく飛んでいる銘柄だけを対象にする"),
    ] = False,
    missing_adjusted: Annotated[
        bool,
        typer.Option(
            "--missing-adjusted",
            help="調整後終値が無い行を持つ銘柄だけを対象にする。--from と組み合わせて遡る",
        ),
    ] = False,
    max_codes: Annotated[
        int | None,
        typer.Option("--max-codes", help="1 回の実行で扱う銘柄数の上限。遡るときに小分けする"),
    ] = None,
) -> None:
    """Yahoo Finance から日次の株価を取得する.

    1 ページ 20 営業日で、リクエストの間は 2 秒空ける。上場中の全銘柄を 1 年ぶん遡ると
    半日では終わらない。日々の更新は --from を省いて差分だけ取る。
    """
    if missing_adjusted and from_date is None:
        # 差分取得の起点は最新取引日なので、NULL の行に届かない。埋めるには遡る起点が要る。
        raise typer.BadParameter("--missing-adjusted には --from が要る")

    settings = get_settings()
    end = date.fromisoformat(to_date) if to_date else date.today()
    default_start = date.fromisoformat(from_date) if from_date is not None else None

    with session_scope(create_session_factory(settings.database_url)) as session:
        targets = _resolve_tick_targets(session, codes, only_jumps, missing_adjusted, default_start)
        if max_codes is not None and len(targets) > max_codes:
            # 遡るときは 1 銘柄で何十ページも叩くため、まとめて流すと Yahoo に締められる。
            logger.info("%d 銘柄のうち先頭 %d 件だけ扱う", len(targets), max_codes)
            targets = targets[:max_codes]
        previous = {} if default_start is not None else latest_prices(session)

        logger.info("株価を取得: %d 銘柄 / 終了日 %s", len(targets), end)
        with create_client() as client:
            saved, failed, splits = _collect_ticks(
                session, client, targets, previous, default_start, end
            )
            if splits:
                saved += _refetch_split_codes(session, client, splits, end)

        if only_jumps:
            _record_remaining_jumps(session, targets)

    logger.info("完了: %d 件保存 / 取得できなかった銘柄 %d 件", saved, failed)


def _resolve_tick_targets(
    session: Session,
    codes: str | None,
    only_jumps: bool,
    missing_adjusted: bool = False,
    since: date | None = None,
) -> list[str]:
    """取得する銘柄を決める."""
    if codes is not None:
        return [code.strip() for code in codes.split(",") if code.strip()]
    if only_jumps:
        jumps = codes_with_price_jumps(session)
        logger.info("調整後終値が飛んでいる銘柄が %d 件", len(jumps))
        return jumps
    if missing_adjusted and since is not None:
        missing = codes_missing_adjusted(session, since)
        logger.info("調整後終値が無い銘柄が %d 件", len(missing))
        return missing
    return listed_codes(session)


def _collect_ticks(
    session: Session,
    client: httpx.Client,
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
            count, split = _fetch_one_code(session, client, code, start, end, watch)
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
    client: httpx.Client,
    code: str,
    start: date,
    end: date,
    watch: tuple[date, Decimal] | None,
) -> tuple[int, bool]:
    """1 銘柄を取り込む. 戻りは (保存件数, 分割を検出したか).

    期間は暦年ごとに区切って要求する。まとめて要求すると 1 銘柄で何十ページも続けて叩く
    ことになり、Yahoo が 500 を返し始める。

    watch は「取り込み済みの最新取引日とその日の調整後終値」。同じ日をもう一度取って値が
    変わっていれば、株式分割で過去まで書き換わったことになる。
    """
    saved = 0
    split = False

    for period_start, period_end in iter_periods(start, end):
        for page in fetch_quotes(client, code, period_start, period_end):
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


def _refetch_split_codes(
    session: Session, client: httpx.Client, codes: list[str], end: date
) -> int:
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
            count, _ = _fetch_one_code(session, client, code, start, end, None)
        except (YahooPageError, httpx.HTTPError) as error:
            logger.warning("%s の取り直しに失敗: %s", code, error)
            continue
        saved += count
        logger.info("%s を %s から取り直した (%d 件)", code, start, count)

    return saved


def _record_remaining_jumps(session: Session, targets: list[str]) -> int:
    """取り直しても消えなかった飛びを tick_jump_checks に記録する.

    低位株の 1 円刻みや、売買が成立しない日が続いた後の値付けは本物の値動きなので、
    取り直しても同じ値が返る。記録しないと毎晩同じ銘柄を取り直し続ける。

    見るのは今回取り直した銘柄だけ。--max-codes で後回しにした銘柄まで記録すると、
    一度も取り直さないまま判定から外れてしまう。
    """
    if not targets:
        return 0

    remaining = price_jumps(session, codes=targets)
    if not remaining:
        return 0

    save_jump_checks(session, remaining)
    session.commit()
    logger.info("取り直しても消えなかった飛びを %d 件記録した", len(remaining))
    return len(remaining)


def _overlap_start(watch: tuple[date, Decimal] | None) -> date:
    """差分取得の開始日. 最新取引日そのものから取り、1 日ぶん重ねる.

    重ねるのは株式分割に気づくため。追加のリクエストは要らない。同じページに載っている。
    取り込みが無い銘柄は 1 年前から取る。
    """
    if watch is None:
        return date.today() - timedelta(days=365)
    return watch[0]


@parse_app.command("edinet")
def parse_edinet(
    max_documents: Annotated[
        int | None,
        typer.Option("--max-documents", help="1 回の実行で解析する書類数の上限"),
    ] = None,
    reparse: Annotated[
        bool,
        typer.Option("--reparse", help="解析済みの書類もやり直す。パーサを直したとき用"),
    ] = False,
    doc_ids: Annotated[
        str | None,
        typer.Option("--doc-id", help="書類管理番号をカンマ区切りで指定する。解析済みでもやり直す"),
    ] = None,
) -> None:
    """取得済みの有価証券報告書を解析して edinet_facts と edinet_shareholders に入れる.

    書類単位で消してから入れ直すので、同じ書類を 2 回解析しても壊れない。ZIP を取り直す
    必要は無い。失敗した書類は parsed_at が空のまま残り、次の実行が拾い直す。

    訂正有報 (130) も同じ手順で解析する。訂正は差分ではなく全文なので、元とマージせず
    書類ごとに丸ごと入れる。期ごとにどれが最新かは edinet_latest_facts が選ぶ。

    ラベルは書類に同梱されたものを ``edinet_document_labels`` に入れる。会社が標準の勘定に
    付けた言い換えが入るので、書類ごとに分けて持つ。金融庁のタクソノミにある標準ラベルは
    ``kabu parse taxonomy`` で別に入れる。
    """
    settings = get_settings()

    with session_scope(create_session_factory(settings.database_url)) as session:
        if doc_ids is not None:
            documents = documents_by_id(
                session, [part.strip() for part in doc_ids.split(",") if part.strip()]
            )
        else:
            documents = unparsed_documents(session, limit=max_documents, include_parsed=reparse)
        if not documents:
            logger.info("解析する書類がありません")
            return

        logger.info("解析する書類が %d 件", len(documents))
        parsed, failed, facts, holders = _parse_documents(
            session, documents, settings.kabu_data_dir
        )

    logger.info(
        "完了: %d 件解析 (失敗 %d 件) / ファクト %d 行 / 大株主 %d 行",
        parsed,
        failed,
        facts,
        holders,
    )


def _parse_documents(
    session: Session, documents: Sequence[EdinetDocument], data_dir: Path
) -> tuple[int, int, int, int]:
    """書類を 1 件ずつ解析して書き込む. 戻りは (成功, 失敗, ファクト行, 大株主行)."""
    parsed = 0
    failed = 0
    total_facts = 0
    total_holders = 0
    consecutive_failures = 0

    for index, document in enumerate(documents, start=1):
        path = document_path(data_dir, document.doc_id, document.submit_date)
        try:
            facts, holders = _parse_one_document(session, document, path)
        except (EdinetXbrlError, zipfile.BadZipFile, OSError, ValueError) as error:
            failed += 1
            consecutive_failures += 1
            logger.warning("%s の解析に失敗: %s", document.doc_id, error)
            session.rollback()
            mark_parsed(session, document.doc_id, error=f"{type(error).__name__}: {error}")
            session.commit()
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                # ZIP の置き場ごと見えていないなど、続けても直らない壊れ方をしている。
                logger.error("%d 件続けて失敗したため中断する", consecutive_failures)
                break
            continue

        session.commit()
        parsed += 1
        total_facts += facts
        total_holders += holders
        consecutive_failures = 0

        if index % 100 == 0:
            logger.info("%d / %d 件 (ファクト %d 行)", index, len(documents), total_facts)

    return parsed, failed, total_facts, total_holders


def _parse_one_document(session: Session, document: EdinetDocument, path: Path) -> tuple[int, int]:
    """1 件を解析して書き込む. コミットは呼び出し側."""
    if not path.exists():
        raise FileNotFoundError(f"ZIP が見つかりません: {path}")

    parsed = parse_document(path)
    facts = save_facts(session, document.doc_id, parsed.facts)
    save_document_labels(session, document.doc_id, parsed.labels)

    # 訂正有報には API が periodEnd を返さない。DEI から読んだ会計年度末で補う。
    fiscal_year_end = parsed.info.fiscal_year_end
    holders = save_shareholders(
        session,
        document.doc_id,
        document.code,
        document.period_end or fiscal_year_end,
        parse_shareholders(path).shareholders,
    )
    mark_parsed(session, document.doc_id, info=parsed.info)
    return facts, holders


@parse_app.command("taxonomy")
def parse_taxonomy(
    year: Annotated[int, typer.Argument(help="タクソノミの年度 (例: 2025)")],
) -> None:
    """金融庁のタクソノミから標準ラベルを取り込んで edinet_labels に入れる.

    ZIP は API では取れない。金融庁の「EDINET タクソノミ及びコードリスト」のページから
    手で落として ``<KABU_DATA_DIR>/edinet_taxonomy/Taxonomy_YYYY.zip`` に置くこと。

    年度をまたいで文言が変わることがある。新しい年度を後から流せば上書きされる。
    """
    settings = get_settings()
    path = taxonomy_path(settings.kabu_data_dir, year)
    if not path.exists():
        raise typer.BadParameter(f"タクソノミの ZIP がありません: {path}")

    logger.info("タクソノミを読む: %s", path)
    labels = parse_taxonomy_labels(path)

    with session_scope(create_session_factory(settings.database_url)) as session:
        saved = save_labels(session, labels)

    logger.info("完了: %d 件のラベルを取り込んだ", saved)


@normalize_app.command("financials")
def normalize_financials(
    max_documents: Annotated[
        int | None,
        typer.Option("--max-documents", help="1 回の実行で名寄せする書類数の上限"),
    ] = None,
    renormalize: Annotated[
        bool,
        typer.Option("--renormalize", help="名寄せ済みの書類もやり直す。項目を直したとき用"),
    ] = False,
) -> None:
    """解析済みのファクトを財務項目に寄せて edinet_financials に入れる.

    ファイルは読まない。``edinet_facts`` から作るので、ZIP を取り直す必要も解析し直す
    必要も無い。書類単位で消してから入れ直すため、2 回流しても壊れない。

    1 書類から複数期が入る。「主要な経営指標等」は 5 期分を載せるので、2026 年の有報から
    2022 年の売上高まで取れる。期ごとにどれを使うかは edinet_latest_financials が選ぶ。

    項目の定義を変えたら ``--renormalize`` で全件を作り直すこと。ファクトはそのまま使う
    ので、解析のやり直しよりずっと速い。
    """
    settings = get_settings()

    with session_scope(create_session_factory(settings.database_url)) as session:
        documents = documents_to_normalize(session, limit=max_documents, renormalize=renormalize)
        if not documents:
            logger.info("名寄せする書類がありません")
            return

        logger.info("名寄せする書類が %d 件", len(documents))
        rows, empty = _normalize_documents(session, documents)

    logger.info(
        "完了: %d 件 / %d 行 (%d 項目) / 1 項目も取れなかった書類 %d 件",
        len(documents),
        rows,
        len(ITEMS),
        empty,
    )


def _normalize_documents(session: Session, documents: Sequence[EdinetDocument]) -> tuple[int, int]:
    """書類を 1 件ずつ名寄せして書き込む. 戻りは (書き込んだ行, 空だった書類数).

    1 項目も取れない書類はありうる。要素名が全部会社独自の拡張になっている場合になる。
    エラーにはせず件数だけ数える。多いようなら項目の定義を足すこと。
    """
    total_rows = 0
    empty = 0

    for index, document in enumerate(documents, start=1):
        facts = load_source_facts(session, document.doc_id, document.is_consolidated)
        values = normalize(facts)
        total_rows += save_financials(session, document.doc_id, values)
        if not values:
            empty += 1
            logger.debug("%s (%s) は 1 項目も取れなかった", document.doc_id, document.code)
        session.commit()

        if index % 500 == 0:
            logger.info("%d / %d 件 (%d 行)", index, len(documents), total_rows)

    return total_rows, empty


@parse_app.command("tdnet")
def parse_tdnet(
    max_disclosures: Annotated[
        int | None,
        typer.Option("--max-disclosures", help="1 回の実行で解析する開示数の上限"),
    ] = None,
    reparse: Annotated[
        bool,
        typer.Option("--reparse", help="解析済みの開示もやり直す。パーサを直したとき用"),
    ] = False,
    doc_ids: Annotated[
        str | None,
        typer.Option("--doc-id", help="書類 ID をカンマ区切りで指定する。解析済みでもやり直す"),
    ] = None,
) -> None:
    """取得済みの決算短信を解析して tdnet_summary_facts と tdnet_statement_facts に入れる.

    ZIP には体系のまったく違う 2 系統が入っている。表紙 (Summary) は tse-ed-t の独自体系で
    **会社予想**が載る。予想は有報に無く、決算短信でしか取れない。添付 (Attachment) は
    jppfs_cor / jpigp_cor と有報とまったく同じ体系で、内訳と円単位の精度がある。

    実績は添付を使うこと。表紙の数値は百万円に丸めてあり、同じ売上高が表紙で 138,877、
    添付で 138,877,139 になる。

    書類単位で消してから入れ直すので、同じ開示を 2 回解析しても壊れない。失敗した開示は
    parsed_at が空のまま残り、次の実行が拾い直す。

    XBRL の付かない短信は最初から対象にしない。決算短信の 1 割ほどが PDF だけで、数値は
    どうやっても取れない。
    """
    settings = get_settings()

    with session_scope(create_session_factory(settings.database_url)) as session:
        if doc_ids is not None:
            disclosures = disclosures_by_id(
                session, [part.strip() for part in doc_ids.split(",") if part.strip()]
            )
        else:
            disclosures = unparsed_disclosures(
                session, limit=max_disclosures, include_parsed=reparse
            )
        if not disclosures:
            logger.info("解析する開示がありません")
            return

        logger.info("解析する開示が %d 件", len(disclosures))
        parsed, failed, summary, statement = _parse_disclosures(
            session, disclosures, settings.kabu_data_dir
        )

    logger.info(
        "完了: %d 件解析 (失敗 %d 件) / 表紙 %d 行 / 財務諸表 %d 行",
        parsed,
        failed,
        summary,
        statement,
    )


def _parse_disclosures(
    session: Session, disclosures: Sequence[TdnetDisclosure], data_dir: Path
) -> tuple[int, int, int, int]:
    """開示を 1 件ずつ解析して書き込む. 戻りは (成功, 失敗, 表紙行, 財務諸表行)."""
    parsed = 0
    failed = 0
    total_summary = 0
    total_statement = 0
    consecutive_failures = 0

    for index, disclosure in enumerate(disclosures, start=1):
        path = disclosure_path(data_dir, disclosure.doc_id, disclosure.disclosed_date, "zip")
        try:
            summary, statement = _parse_one_disclosure(session, disclosure, path)
        except (TdnetXbrlError, zipfile.BadZipFile, OSError, ValueError) as error:
            failed += 1
            consecutive_failures += 1
            logger.warning("%s の解析に失敗: %s", disclosure.doc_id, error)
            session.rollback()
            mark_tdnet_parsed(session, disclosure.doc_id, error=f"{type(error).__name__}: {error}")
            session.commit()
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                # ZIP の置き場ごと見えていないなど、続けても直らない壊れ方をしている。
                logger.error("%d 件続けて失敗したため中断する", consecutive_failures)
                break
            continue

        session.commit()
        parsed += 1
        total_summary += summary
        total_statement += statement
        consecutive_failures = 0

        if index % 500 == 0:
            logger.info(
                "%d / %d 件 (表紙 %d 行 / 財務諸表 %d 行)",
                index,
                len(disclosures),
                total_summary,
                total_statement,
            )

    return parsed, failed, total_summary, total_statement


def _parse_one_disclosure(
    session: Session, disclosure: TdnetDisclosure, path: Path
) -> tuple[int, int]:
    """1 件を解析して書き込む. コミットは呼び出し側.

    ZIP が無いのは失敗ではない。決算短信の 1 割ほどに XBRL が付かず、PDF だけになる。
    解析済みとして片付け、次の実行が拾い直さないようにする。
    """
    if not path.exists():
        mark_no_xbrl(session, disclosure.doc_id)
        return 0, 0

    parsed = parse_disclosure(path)
    summary = save_summary_facts(session, disclosure.doc_id, parsed.summary_facts)
    statement = save_statement_facts(session, disclosure.doc_id, parsed.statement_facts)
    mark_tdnet_parsed(session, disclosure.doc_id, info=complete_info(parsed.info, disclosure.title))
    return summary, statement


@normalize_app.command("tdnet-financials")
def normalize_tdnet_financials(
    max_disclosures: Annotated[
        int | None,
        typer.Option("--max-disclosures", help="1 回の実行で名寄せする開示数の上限"),
    ] = None,
    renormalize: Annotated[
        bool,
        typer.Option("--renormalize", help="名寄せ済みの開示もやり直す。項目を直したとき用"),
    ] = False,
) -> None:
    """解析済みの短信の添付を財務項目に寄せて tdnet_financials に入れる.

    項目の定義は有報と共有する。添付は jppfs_cor / jpigp_cor と有報と同じ体系なので、
    ``normalizers.financials.ITEM_SPECS`` がそのまま効く。有報が年 1 回なのに対し、
    こちらは四半期ごとに入るので粒度が上がる。

    有報と違って period_kind を持つ。同じ期末に年初来累計 (ytd) と単独四半期 (quarter) が
    並ぶため。「Q2 累計 100 億」と「Q2 単独 50 億」は別物になる。

    ファイルは読まない。tdnet_statement_facts から作るので、ZIP を取り直す必要も解析し直す
    必要も無い。書類単位で消してから入れ直すため、2 回流しても壊れない。
    """
    settings = get_settings()

    with session_scope(create_session_factory(settings.database_url)) as session:
        disclosures = disclosures_to_normalize(
            session, limit=max_disclosures, renormalize=renormalize
        )
        if not disclosures:
            logger.info("名寄せする開示がありません")
            return

        logger.info("名寄せする開示が %d 件", len(disclosures))
        rows, empty = _normalize_tdnet_documents(session, disclosures)

    logger.info(
        "完了: %d 件 / %d 行 (%d 項目) / 1 項目も取れなかった開示 %d 件",
        len(disclosures),
        rows,
        len(ITEMS),
        empty,
    )


def _normalize_tdnet_documents(
    session: Session, disclosures: Sequence[TdnetDisclosure]
) -> tuple[int, int]:
    """開示を 1 件ずつ名寄せして書き込む. 戻りは (書き込んだ行, 空だった開示数).

    1 項目も取れない開示はありうる。添付にセグメント情報しか入っていない短信がある。
    エラーにはせず件数だけ数える。
    """
    total_rows = 0
    empty = 0
    from_summary = 0

    for index, disclosure in enumerate(disclosures, start=1):
        facts = load_statement_source_facts(session, disclosure.doc_id, disclosure.is_consolidated)
        values = normalize_tdnet(facts)
        if not values:
            # 添付が空なら表紙で代える。米国基準の会社は添付 XBRL を出さず、数値データの
            # 訂正短信も表紙だけを出し直す。表紙の値は百万円に丸めてあるので、添付が
            # 取れているうちは使わない。
            summary = load_summary_source_facts(
                session, disclosure.doc_id, disclosure.is_consolidated
            )
            values = normalize_tdnet_summary(summary)
            if values:
                from_summary += 1

        total_rows += save_tdnet_financials(session, disclosure.doc_id, values)
        if not values:
            empty += 1
            logger.debug("%s (%s) は 1 項目も取れなかった", disclosure.doc_id, disclosure.code)
        session.commit()

        if index % 500 == 0:
            logger.info("%d / %d 件 (%d 行)", index, len(disclosures), total_rows)

    if from_summary:
        logger.info("添付が空のため表紙から取った開示が %d 件", from_summary)

    return total_rows, empty
