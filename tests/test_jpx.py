"""JPX 銘柄一覧の正規化のテスト."""

import pytest

from kabu_app.collectors.jpx import _build_stock

BASE_RECORD = {
    "日付": "20260630",
    "コード": "1301",
    "銘柄名": "極洋",
    "市場・商品区分": "プライム（内国株式）",
    "33業種コード": "50",
    "33業種区分": "水産・農林業",
    "17業種コード": "1",
    "17業種区分": "食品",
    "規模コード": "6",
    "規模区分": "TOPIX Small 1",
}


def test_業種コードを0埋めする() -> None:
    """Excel が数値セルで持つため先頭の 0 が落ちる。33 業種の 0050 が 50 になる."""
    stock = _build_stock(dict(BASE_RECORD))
    assert stock.industry33_code == "0050"
    assert stock.industry17_code == "01"


def test_4桁ある業種コードはそのまま() -> None:
    stock = _build_stock(BASE_RECORD | {"33業種コード": "1050", "17業種コード": "17"})
    assert stock.industry33_code == "1050"
    assert stock.industry17_code == "17"


def test_市場区分を英語のコードに変換する() -> None:
    for jpx, expected in [
        ("プライム（内国株式）", "prime"),
        ("スタンダード（内国株式）", "standard"),
        ("グロース（内国株式）", "growth"),
    ]:
        assert _build_stock(BASE_RECORD | {"市場・商品区分": jpx}).market_segment == expected


def test_規模区分のハイフンはNoneになる() -> None:
    """規模コードは 3716 件中 2079 件が "-"。文字列のまま入れない."""
    stock = _build_stock(BASE_RECORD | {"規模コード": "-", "規模区分": "-"})
    assert stock.topix_scale_code is None
    assert stock.topix_scale_name is None


def test_優先株の5桁コードを受け入れる() -> None:
    stock = _build_stock(BASE_RECORD | {"コード": "25935", "銘柄名": "伊藤園第１種優先株式"})
    assert stock.code == "25935"


def test_英字混じりのコードを受け入れる() -> None:
    assert _build_stock(BASE_RECORD | {"コード": "130A"}).code == "130A"


def test_業種コードが数字でなければエラー() -> None:
    with pytest.raises(ValueError, match="数字ではありません"):
        _build_stock(BASE_RECORD | {"33業種コード": "50A"})


def test_業種名が空ならエラー() -> None:
    """3 市場の内国株では業種が必ず埋まる。空なら黙って通さない."""
    with pytest.raises(ValueError, match="33業種区分 が空です"):
        _build_stock(BASE_RECORD | {"33業種区分": "-"})
