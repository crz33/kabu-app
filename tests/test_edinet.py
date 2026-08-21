"""EDINET 書類一覧の正規化のテスト."""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from kabu_app.collectors.edinet import (
    EdinetApiError,
    _build_meta,
    _raise_for_api_error,
    document_path,
    to_stock_code,
)

BASE_RECORD: dict[str, Any] = {
    "seqNumber": 640,
    "docID": "S100YW7F",
    "edinetCode": "E05729",
    "secCode": "21680",
    "filerName": "株式会社パソナグループ",
    "docTypeCode": "120",
    "periodStart": "2025-06-01",
    "periodEnd": "2026-05-31",
    "submitDateTime": "2026-08-14 16:00",
    "docDescription": "有価証券報告書－第49期",
    "parentDocID": None,
    "withdrawalStatus": "0",
    "xbrlFlag": "1",
}


def test_証券コードの末尾0を落として4桁にする() -> None:
    """EDINET は 5 桁に 0 埋めする。JPX の 1301 は EDINET では 13010."""
    assert to_stock_code("13010") == "1301"


def test_英字混じりのコードも4桁になる() -> None:
    """2024 年から英字を含むコードが割り当てられている."""
    assert to_stock_code("130A0") == "130A"


def test_末尾が0でない5桁はそのまま返す() -> None:
    """優先株のような枝番付きのコード。落とすと別の銘柄になる."""
    assert to_stock_code("25935") == "25935"


def test_有報を正規化する() -> None:
    meta = _build_meta(dict(BASE_RECORD))
    assert meta is not None
    assert meta.doc_id == "S100YW7F"
    assert meta.edinet_code == "E05729"
    assert meta.sec_code == "21680"
    assert meta.code == "2168"
    assert meta.period_end == date(2026, 5, 31)
    assert meta.has_xbrl is True
    assert meta.is_withdrawn is False


def test_提出日は提出日時から取る() -> None:
    """submitDateTime は "YYYY-MM-DD HH:MM"。秒もタイムゾーンも無い."""
    meta = _build_meta(dict(BASE_RECORD))
    assert meta is not None
    assert meta.submitted_at == datetime(2026, 8, 14, 16, 0)
    assert meta.submit_date == date(2026, 8, 14)


def test_訂正有報は親の書類を指し対象期間を持たない() -> None:
    """130 には periodEnd が入らない。どの期の訂正かは parent_doc_id を辿る."""
    meta = _build_meta(
        BASE_RECORD | {"docTypeCode": "130", "periodEnd": None, "parentDocID": "S100WU0J"}
    )
    assert meta is not None
    assert meta.doc_type_code == "130"
    assert meta.period_end is None
    assert meta.parent_doc_id == "S100WU0J"


def test_対象外の書類種別は捨てる() -> None:
    """臨時報告書 (180) や大量保有報告書 (350) が同じ一覧に混ざっている."""
    assert _build_meta(BASE_RECORD | {"docTypeCode": "180"}) is None


def test_証券コードが無い提出者は捨てる() -> None:
    """非上場の会社やファンドも有報を出す。投資判断の対象にならない."""
    assert _build_meta(BASE_RECORD | {"secCode": None}) is None
    assert _build_meta(BASE_RECORD | {"secCode": ""}) is None


def test_取り下げた書類にフラグが立つ() -> None:
    """0 が通常、1 が取り下げた書類、2 が取り下げられた書類."""
    for status in ("1", "2"):
        meta = _build_meta(BASE_RECORD | {"withdrawalStatus": status})
        assert meta is not None
        assert meta.is_withdrawn is True


def test_XBRLの無い書類にフラグが立つ() -> None:
    meta = _build_meta(BASE_RECORD | {"xbrlFlag": "0"})
    assert meta is not None
    assert meta.has_xbrl is False


def test_必須の項目が欠けていればエラー() -> None:
    with pytest.raises(ValueError, match="メタデータが欠けています"):
        _build_meta(BASE_RECORD | {"docID": None})


def test_認証エラーは本文を見ないと気づけない() -> None:
    """キーが無効でも HTTP は 200。metadata ごと無く StatusCode に 401 が入る."""
    payload = {
        "StatusCode": 401,
        "message": "Access denied due to invalid subscription key.",
    }
    with pytest.raises(EdinetApiError, match="401"):
        _raise_for_api_error(payload, date(2026, 8, 14))


def test_想定外のステータスでもエラーにする() -> None:
    with pytest.raises(EdinetApiError, match="404"):
        _raise_for_api_error({"metadata": {"status": "404", "message": "Not Found"}}, date.today())


def test_正常な応答は素通りする() -> None:
    _raise_for_api_error({"metadata": {"status": "200"}, "results": []}, date.today())


def test_保存先は提出日ごとのディレクトリになる() -> None:
    path = document_path(Path("/mnt/usb/data"), "S100YW7F", date(2026, 8, 14))
    assert path == Path("/mnt/usb/data/edinet/20260814/S100YW7F.zip")
