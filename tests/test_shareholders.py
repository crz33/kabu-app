"""大株主の抽出とオーナー判定のテスト."""

import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from kabu_app.parsers.shareholders import (
    ShareholdersResult,
    classify,
    parse_shareholders,
)


def _classify(name: str, directors: set[str] | None = None, surnames: set[str] | None = None):
    return classify(name, directors or set(), surnames or set())


def test_個人はオーナー系になる() -> None:
    assert _classify("山田太郎") == ("individual", True)


def test_法人格を含めば個人ではない() -> None:
    assert _classify("株式会社サンプル商事") == ("corporate", False)


def test_信託銀行は名義人として外す() -> None:
    """実質の持ち主は別にいる。オーナー家の持ち分に足すと過大になる."""
    kind, is_owner = _classify("日本マスタートラスト信託銀行株式会社（信託口）")
    assert (kind, is_owner) == ("trust", False)


def test_信託銀行は役員の姓と一致しても名義人のまま() -> None:
    """三井住友信託銀行は、役員に三井さんがいてもオーナー家のビークルではない."""
    kind, is_owner = _classify("三井住友信託銀行株式会社", surnames={"三井"})
    assert (kind, is_owner) == ("trust", False)


def test_持株会はオーナー系に入れない() -> None:
    assert _classify("サンプル社員持株会") == ("employee", False)


def test_国の保有はオーナー系に入れない() -> None:
    assert _classify("財務大臣") == ("public", False)


def test_有限会社は資産管理会社とみなす() -> None:
    assert _classify("有限会社酒東商事") == ("asset_mgmt", True)


def test_役員の姓を冠する法人は資産管理会社とみなす() -> None:
    """公益財団法人「河内」奨学財団のような一族のビークルを拾う."""
    assert _classify("公益財団法人河内奨学財団", surnames={"河内"}) == ("asset_mgmt", True)


def test_役員本人と一致すれば役員として扱う() -> None:
    assert _classify("山田 太郎", directors={"山田太郎"}) == ("director", True)


def test_役員と同姓の金融機関は姓の一致から外す() -> None:
    """松井証券は、役員に松井さんがいても一族のビークルではない.

    政策保有の金融機関は割合が大きく、オーナー比率に足すと大きく狂う。
    """
    assert _classify("松井証券株式会社", surnames={"松井"}) == ("corporate", False)


def test_均等割り付けされた役員名から姓を取る() -> None:
    """有報の役員名は「南 部　真 希 也」のように 1 文字ずつ空けて刷られる.

    空白で分けると「南」しか残らず、姓の一致が働かなくなる。
    """
    from kabu_app.parsers.shareholders import _surnames

    assert "南部" in _surnames(["南 部　真 希 也"])


def test_姓の一致で一族の資産管理会社を拾う() -> None:
    """三谷家のビークル。株主名に社名の一部として姓が入る."""
    surnames = _surnames_of("三 谷　忠 照")

    assert _classify("三谷産業株式会社", surnames=surnames) == ("asset_mgmt", True)
    assert _classify("公益財団法人三谷育英会", surnames=surnames) == ("asset_mgmt", True)


def _surnames_of(*directors: str) -> set[str]:
    from kabu_app.parsers.shareholders import _surnames

    return _surnames(list(directors))


def test_オーナー比率はオーナー系だけを合算する() -> None:
    result = ShareholdersResult(
        shareholders=(
            _holder(1, "山田太郎", "30.00", "individual", True),
            _holder(2, "日本カストディ銀行（信託口）", "20.00", "trust", False),
            _holder(3, "有限会社山田興産", "5.00", "asset_mgmt", True),
        )
    )

    assert result.owner_ratio == Decimal("35.00")
    assert result.top_ratio == Decimal("55.00")
    assert result.is_owner_company is True


def test_大株主が取れなければ比率はNone() -> None:
    """0% と「取れなかった」を混ぜない."""
    result = ShareholdersResult()

    assert result.owner_ratio is None
    assert result.is_owner_company is None


def _holder(rank: int, name: str, ratio: str, kind: str, is_owner: bool):
    from kabu_app.parsers.shareholders import Shareholder

    return Shareholder(
        rank=rank, name=name, shares=None, ratio=Decimal(ratio), kind=kind, is_owner=is_owner
    )


_INSTANCE = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2025-11-01/jpcrp_cor">
  <jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors contextRef="c">山田 太郎</jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors>
  <jpcrp_cor:NameMajorShareholders contextRef="CurrentYearInstant_No1MajorShareholdersMember">山田 太郎</jpcrp_cor:NameMajorShareholders>
  <jpcrp_cor:NumberOfSharesHeld contextRef="CurrentYearInstant_No1MajorShareholdersMember">1,234,000</jpcrp_cor:NumberOfSharesHeld>
  <jpcrp_cor:ShareholdingRatio contextRef="CurrentYearInstant_No1MajorShareholdersMember">0.1164</jpcrp_cor:ShareholdingRatio>
  <jpcrp_cor:NameMajorShareholders contextRef="CurrentYearInstant_No2MajorShareholdersMember">日本カストディ銀行株式会社（信託口）</jpcrp_cor:NameMajorShareholders>
  <jpcrp_cor:ShareholdingRatio contextRef="CurrentYearInstant_No2MajorShareholdersMember">0.0520</jpcrp_cor:ShareholdingRatio>
  <jpcrp_cor:NameMajorShareholders contextRef="Prior1YearInstant_No1MajorShareholdersMember">前期の株主</jpcrp_cor:NameMajorShareholders>
</xbrli:xbrl>
"""


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "S100TEST.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E00000.xbrl", _INSTANCE)
    return path


def test_当期の大株主を順位順に取る(document: Path) -> None:
    """前期の大株主は取らない。当期の状況だけが欲しい."""
    holders = parse_shareholders(document).shareholders

    assert [h.rank for h in holders] == [1, 2]
    assert [h.name for h in holders] == [
        "山田 太郎",
        "日本カストディ銀行株式会社（信託口）",
    ]


def test_保有割合を百分率に直す(document: Path) -> None:
    """XBRL には小数で入っている。0.1164 は 11.64%."""
    assert parse_shareholders(document).shareholders[0].ratio == Decimal("11.64")


def test_株式数のカンマを落として整数にする(document: Path) -> None:
    assert parse_shareholders(document).shareholders[0].shares == 1234000


def test_株式数が無ければNone(document: Path) -> None:
    assert parse_shareholders(document).shareholders[1].shares is None


def test_役員名と一致する株主は役員として扱う(document: Path) -> None:
    assert parse_shareholders(document).shareholders[0].kind == "director"
