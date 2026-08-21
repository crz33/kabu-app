"""Yahoo Finance の株価ページの読み取りのテスト."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from kabu_app.collectors import yahoo
from kabu_app.collectors.yahoo import (
    YahooPageError,
    _build_quote,
    _extract_histories,
    _request,
    count_pages,
)


def _entry(
    day: str = "2026-08-21",
    values: list[str] | None = None,
) -> dict[str, Any]:
    if values is None:
        values = ["3,066", "3,132", "3,056", "3,132", "25,924,500", "3,132", "11.79", "0.99"]
    return {"date": day, "values": [{"value": value} for value in values]}


def _page(entries: list[dict[str, Any]], total_size: int) -> bytes:
    """RSC ペイロードを積んだページを組み立てる.

    実物は push([1, "<JSON 文字列>"]) の二重エンコードで、文字列の頭に "a:" のような
    印が付く。取り出す側は最初の "[" から読む。
    """
    payload = [{"pager": {"totalSize": total_size}, "histories": entries}]
    inner = "a:" + json.dumps(payload, ensure_ascii=False)
    script = f"self.__next_f.push([1,{json.dumps(inner, ensure_ascii=False)}])"
    return f"<html><body><script>{script}</script></body></html>".encode()


def test_株価と総件数を取り出す() -> None:
    histories, total_size = _extract_histories(_page([_entry()], 36), "7203")

    assert total_size == 36
    assert len(histories) == 1


def test_ページの作りが変わったら例外() -> None:
    """RSC が見つからないのは、上場廃止でページが消えた場合も含む."""
    with pytest.raises(YahooPageError, match="7203"):
        _extract_histories(b"<html><body><p>404</p></body></html>", "7203")


def test_四本値と調整後終値を読む() -> None:
    quote = _build_quote("7203", _entry())

    assert quote is not None
    assert quote.date == date(2026, 8, 21)
    assert quote.open == Decimal("3066")
    assert quote.high == Decimal("3132")
    assert quote.low == Decimal("3056")
    assert quote.close == Decimal("3132")
    assert quote.volume == 25924500
    assert quote.adjusted_close == Decimal("3132")


def test_分割した銘柄は調整後終値が終値と違う() -> None:
    """2961 は 2026-07-30 に 4 分割した。7/23 の終値 6330 に対し調整後は 1582.5."""
    quote = _build_quote(
        "2961", _entry("2026-07-23", ["6,300", "6,330", "6,300", "6,330", "300", "1,582.5"])
    )

    assert quote is not None
    assert quote.close == Decimal("6330")
    assert quote.adjusted_close == Decimal("1582.5")


def test_小数の株価を落とさない() -> None:
    """0.1 円単位の値が実在する。整数に丸めると誤差が出る."""
    quote = _build_quote(
        "7203", _entry(values=["2,938", "2,950", "2,913.5", "2,950", "24,503,500", "2,950"])
    )

    assert quote is not None
    assert quote.low == Decimal("2913.5")


def test_売買が成立しなかった日は捨てる() -> None:
    """値が付かない日は "---" になる."""
    quote = _build_quote("7203", _entry(values=["---", "---", "---", "---", "---", "---"]))

    assert quote is None


def test_列が足りない行は捨てる() -> None:
    """調整後終値まで無い行は使えない."""
    assert _build_quote("7203", _entry(values=["100", "100", "100", "100", "1"])) is None


def test_日付が読めない行は捨てる() -> None:
    assert _build_quote("7203", _entry(day="2026年8月21日")) is None


def test_スラッシュ区切りの日付も読む() -> None:
    """指数のページはこちらの書式で返る."""
    quote = _build_quote("7203", _entry(day="2026/8/21"))

    assert quote is not None
    assert quote.date == date(2026, 8, 21)


def test_総件数からページ数を出す() -> None:
    """1 ページ 20 営業日。端数は 1 ページ数える."""
    assert count_pages(0) == 0
    assert count_pages(20) == 1
    assert count_pages(21) == 2
    assert count_pages(36) == 2


def _client(responses: list[int]) -> tuple[httpx.Client, list[int]]:
    """指定したステータスを順に返すクライアント。呼ばれた回数を数えられる."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = responses[min(len(calls), len(responses) - 1)]
        calls.append(status)
        return httpx.Response(status, content=b"<html></html>")

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_一時的な500は待って掛け直す(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yahoo は数分にわたって 500 を返すことがある。全銘柄を回す途中だと総崩れになる."""
    monkeypatch.setattr(yahoo.time, "sleep", lambda _: None)
    client, calls = _client([500, 500, 200])

    response = _request(client, "https://example.test/", {"page": "1"})

    assert response.status_code == 200
    assert calls == [500, 500, 200]


def test_回復しなければ例外にする(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yahoo.time, "sleep", lambda _: None)
    client, calls = _client([500])

    with pytest.raises(httpx.HTTPStatusError):
        _request(client, "https://example.test/", {"page": "1"})

    assert len(calls) == 4


def test_404は掛け直さない(monkeypatch: pytest.MonkeyPatch) -> None:
    """作りが変わったり銘柄が消えたりした場合。待っても直らない."""
    monkeypatch.setattr(yahoo.time, "sleep", lambda _: None)
    client, calls = _client([404])

    with pytest.raises(httpx.HTTPStatusError):
        _request(client, "https://example.test/", {"page": "1"})

    assert len(calls) == 1


def test_期間を暦年ごとに切る() -> None:
    """1 銘柄で何十ページも続けて叩くと Yahoo が 500 を返し始める."""
    assert list(yahoo.iter_periods(date(2024, 1, 4), date(2026, 8, 21))) == [
        (date(2024, 1, 4), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 8, 21)),
    ]


def test_同じ年に収まるなら切らない() -> None:
    assert list(yahoo.iter_periods(date(2026, 8, 15), date(2026, 8, 21))) == [
        (date(2026, 8, 15), date(2026, 8, 21))
    ]
