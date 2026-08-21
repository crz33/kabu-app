"""Yahoo Finance から日次の株価を取得する.

このモジュールは DB を知らない。取得と正規化だけを行い、結果を dataclass で返す。

API は無い。株価時系列のページを読む。中身は Next.js の RSC ペイロードに JSON で載って
いるので、HTML の DOM ではなくそちらを解く。1 ページ 20 営業日で、ページ番号で遡る。
"""

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from lxml import html as lxml_html

logger = logging.getLogger(__name__)

YAHOO_HISTORY_URL = "https://finance.yahoo.co.jp/quote/{code}.T/history"

PAGE_SIZE = 20
"""1 ページに載る営業日数."""

REQUEST_INTERVAL = 2.0
"""連続してリクエストするときに空ける秒数。スクレイピングなので長めに取る."""

_MAX_PAGES = 200
"""ページを舐めるときの歯止め. 20 営業日 x 200 で 16 年ぶん."""

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_RSC_PATTERN = re.compile(r"push\(\[1,(.+)\]\)\s*$", re.DOTALL)
_MISSING_MARKS = frozenset({"", "---", "-"})

# values の並び。6 番目以降の PER と PBR は直近の日にしか値が入らないので使わない。
_OPEN, _HIGH, _LOW, _CLOSE, _VOLUME, _ADJUSTED = range(6)


class YahooPageError(RuntimeError):
    """ページから株価を取り出せなかった. 作りが変わった可能性がある."""


@dataclass(frozen=True, slots=True)
class DailyQuote:
    """ある銘柄のある日の四本値。フィールド名は ticks の列名に合わせてある."""

    code: str
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted_close: Decimal


def fetch_quotes(
    code: str, start: date, end: date, timeout: float = 30.0
) -> Iterator[list[DailyQuote]]:
    """指定期間の日次株価をページごとに返す.

    ページ 1 が最新で、番号が増えるほど古くなる。呼び出し側はページごとに書き込めるので、
    途中で止まっても取れたぶんは残る。

    Raises:
        YahooPageError: ページの作りが変わって株価を取り出せなかった場合
        httpx.HTTPError: 通信に失敗した場合
    """
    params = {
        "styl": "stock",
        "from": f"{start:%Y%m%d}",
        "to": f"{end:%Y%m%d}",
        "timeFrame": "d",
    }
    url = YAHOO_HISTORY_URL.format(code=code)

    with httpx.Client(
        headers={"User-Agent": _USER_AGENT}, timeout=timeout, follow_redirects=True
    ) as client:
        total_pages = _MAX_PAGES
        for page in range(1, _MAX_PAGES + 1):
            response = client.get(url, params={**params, "page": str(page)})
            response.raise_for_status()
            histories, total_size = _extract_histories(response.content, code)

            if page == 1:
                if total_size == 0:
                    return
                total_pages = min(-(-total_size // PAGE_SIZE), _MAX_PAGES)

            quotes = [
                quote for entry in histories if (quote := _build_quote(code, entry)) is not None
            ]
            if quotes:
                yield quotes

            if page >= total_pages:
                return


def count_pages(total_size: int) -> int:
    """件数からページ数を出す. 取得前に規模を見積もるために使う."""
    return -(-total_size // PAGE_SIZE)


def _extract_histories(content: bytes, code: str) -> tuple[list[dict[str, Any]], int]:
    """RSC ペイロードから株価の配列と総件数を取り出す."""
    tree = lxml_html.fromstring(content.decode("utf-8", errors="replace"))

    for script in tree.iter("script"):
        text = script.text
        if text is None or "__next_f" not in text or "histories" not in text:
            continue
        match = _RSC_PATTERN.search(text)
        if match is None:
            continue
        try:
            # push([1, "<JSON 文字列>"]) の二重エンコード。外側を解いてから中身を解く。
            decoded: str = json.loads(match.group(1))
            payload = json.loads(decoded[decoded.index("[") :])
        except (ValueError, json.JSONDecodeError):
            continue

        histories = _find_key(payload, "histories")
        if histories is None:
            continue
        pager = _find_key(payload, "pager") or {}
        return list(histories), int(pager.get("totalSize", 0))

    # 上場廃止した銘柄はページ自体が消えている。株価が 1 件も無いのと区別できない。
    raise YahooPageError(f"{code}: 株価を取り出せませんでした")


def _find_key(obj: Any, key: str) -> Any:
    """入れ子の JSON から最初に見つかったキーの値を返す.

    RSC の構造は深く、途中の階層名が変わりうる。キーの名前だけを頼りに探す。
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            if (found := _find_key(value, key)) is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            if (found := _find_key(item, key)) is not None:
                return found
    return None


def _build_quote(code: str, entry: dict[str, Any]) -> DailyQuote | None:
    """1 日ぶんの行を正規化する. 値が欠けていれば None."""
    values = entry.get("values") or []
    if len(values) <= _ADJUSTED:
        return None

    traded_on = _parse_date(entry.get("date"))
    numbers = [_parse_decimal(values[index].get("value")) for index in range(_ADJUSTED + 1)]
    if traded_on is None or any(number is None for number in numbers):
        return None

    open_, high, low, close, volume, adjusted = numbers
    assert open_ is not None and high is not None and low is not None
    assert close is not None and volume is not None and adjusted is not None

    return DailyQuote(
        code=code,
        date=traded_on,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=int(volume),
        adjusted_close=adjusted,
    )


def _parse_date(value: Any) -> date | None:
    """日付は "2026-08-21" か "2026/8/21" で来る."""
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(value: Any) -> Decimal | None:
    """カンマ区切りの数字。売買が成立しなかった日は "---" になる."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in _MISSING_MARKS:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
