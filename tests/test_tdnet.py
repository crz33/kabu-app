"""TDnet 一覧の正規化のテスト."""

from datetime import date, time
from pathlib import Path

from lxml import html as lxml_html

from kabu_app.collectors.tdnet import (
    _parse_rows,
    disclosure_path,
    is_target_disclosure,
    to_stock_code,
)

DAY = date(2026, 8, 14)


def _table(*rows: str) -> lxml_html.HtmlElement:
    body = "".join(rows)
    return lxml_html.fromstring(
        f"<html><body><table id='main-list-table'>{body}</table></body></html>"
    )


def _row(
    title: str = "2026年5月期 決算短信〔日本基準〕（連結）",
    sec_code: str = "21680",
    pdf: str = "140120260814521503.pdf",
    xbrl: str | None = "081220260814521503.zip",
    markets: str = "東",
    at: str = "15:00",
) -> str:
    xbrl_cell = (
        f'<div class="xbrl-mask"><div class="xbrl-button">'
        f'<a class="style002" href="{xbrl}">XBRL</a></div></div>'
        if xbrl
        else ""
    )
    return (
        f"<tr><td>{at}</td><td>{sec_code}</td><td>パソナグループ</td>"
        f'<td><a href="{pdf}" target="_blank">{title}</a></td>'
        f"<td>{xbrl_cell}</td><td>{markets}</td><td></td></tr>"
    )


def test_証券コードの末尾0を落として4桁にする() -> None:
    assert to_stock_code("21680") == "2168"


def test_英字混じりのコードも4桁になる() -> None:
    """2024 年以降に上場した銘柄。252A0 は Ｐ－ウェッジ."""
    assert to_stock_code("252A0") == "252A"


def test_決算短信を取り込む() -> None:
    rows, found = _parse_rows(_table(_row()), DAY)

    assert rows == 1
    assert len(found) == 1
    meta = found[0]
    assert meta.doc_id == "140120260814521503"
    assert meta.disclosed_date == DAY
    assert meta.disclosed_time == time(15, 0)
    assert meta.sec_code == "21680"
    assert meta.code == "2168"
    assert meta.markets == "東"
    assert meta.xbrl_file == "081220260814521503.zip"
    assert meta.is_amendment is False


def test_決算短信でない開示は捨てる() -> None:
    """一覧には業績予想の修正や配当のお知らせも並ぶ."""
    _, found = _parse_rows(_table(_row(title="通期業績予想の修正に関するお知らせ")), DAY)

    assert found == []


def test_訂正短信も取り込む() -> None:
    """訂正を捨てると 31 日で取り返しがつかなくなる。数値が直った事実も追えない."""
    title = (
        "（訂正・数値データ訂正）"
        "「2026年9月期 第1四半期決算短信〔日本基準〕（連結）」の一部訂正について"
    )
    _, found = _parse_rows(_table(_row(title=title)), DAY)

    assert len(found) == 1
    assert found[0].is_amendment is True


def test_XBRLの無い決算短信も取り込む() -> None:
    """中間決算短信に目立つ。捨てると 1 割ほどの短信が欠ける."""
    _, found = _parse_rows(_table(_row(xbrl=None)), DAY)

    assert len(found) == 1
    assert found[0].xbrl_file is None


def test_複数の市場に上場していれば略号が並ぶ() -> None:
    _, found = _parse_rows(_table(_row(markets="東札福")), DAY)

    assert found[0].markets == "東札福"


def test_テーブルの行数も返す() -> None:
    """決算短信が 0 件でも、行があれば次のページを見に行く必要がある."""
    rows, found = _parse_rows(_table(_row(title="配当予想の修正に関するお知らせ"), _row()), DAY)

    assert rows == 2
    assert len(found) == 1


def test_一覧のテーブルが無ければ空() -> None:
    """31 日より前の日付は、テーブルの無いページが返る."""
    document = lxml_html.fromstring("<html><body><p>該当データはありません</p></body></html>")

    assert _parse_rows(document, DAY) == (0, [])


def test_決算短信の判定は表題に含まれるかで見る() -> None:
    assert is_target_disclosure("2026年5月期 決算短信〔日本基準〕（連結）") is True
    assert is_target_disclosure("決算短信の発表日変更に関するお知らせ") is True
    assert is_target_disclosure("自己株式の取得状況に関するお知らせ") is False


def test_保存先は開示日ごとのディレクトリになる() -> None:
    path = disclosure_path(Path("/mnt/usb/data"), "140120260814521503", DAY, "zip")

    assert path == Path("/mnt/usb/data/tdnet/20260814/140120260814521503.zip")
