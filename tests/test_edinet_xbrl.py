"""有報 XBRL の解析のテスト.

実際の ZIP は 1 本 2MB あり、リポジトリに置くには重い。同じ形の XBRL を組み立てて
振る舞いを確かめる。ここで押さえるのは、実データで踏んだ落とし穴の再発だけにする。
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from kabu_app.parsers.edinet_xbrl import EdinetXbrlError, parse_document

_PL_ROLE = "http://disclosure.edinet-fsa.go.jp/role/jppfs/rol_ConsolidatedStatementOfIncome"
_PL_STANDALONE_ROLE = "http://disclosure.edinet-fsa.go.jp/role/jppfs/rol_StatementOfIncome"


def _instance(contexts: str, facts: str, dei: str = "") -> str:
    """XBRL インスタンスを組み立てる."""
    default_dei = """
        <jpdei_cor:SecurityCodeDEI contextRef="c">75500</jpdei_cor:SecurityCodeDEI>
        <jpdei_cor:FilerNameInJapaneseDEI contextRef="c">試験株式会社</jpdei_cor:FilerNameInJapaneseDEI>
        <jpdei_cor:AccountingStandardsDEI contextRef="c">Japan GAAP</jpdei_cor:AccountingStandardsDEI>
        <jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI contextRef="c">true</jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI>
        <jpdei_cor:CurrentFiscalYearEndDateDEI contextRef="c">2026-03-31</jpdei_cor:CurrentFiscalYearEndDateDEI>
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:jpdei_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpdei/2013-08-31/jpdei_cor"
    xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  {contexts}
  {dei or default_dei}
  {facts}
</xbrli:xbrl>
"""


def _context(context_id: str, *, start: str | None = None, end: str) -> str:
    period = (
        f"<xbrli:instant>{end}</xbrli:instant>"
        if start is None
        else f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
    )
    return f"""
  <xbrli:context id="{context_id}">
    <xbrli:entity>
      <xbrli:identifier scheme="http://disclosure.edinet-fsa.go.jp">E00000</xbrli:identifier>
    </xbrli:entity>
    <xbrli:period>{period}</xbrli:period>
  </xbrli:context>"""


def _presentation(
    role: str,
    concepts: list[str],
    repeat: str | None = None,
    arcs: list[tuple[str, str, float]] | None = None,
) -> str:
    """表示リンクを組み立てる.

    ``repeat`` に渡した勘定は locator を 2 つ持たせる。同じ勘定に複数の locator が振られる
    実際の形を再現するため。``arcs`` は (親, 子, order) で親子関係を作る。
    """
    labels = {concept: f"l{index}" for index, concept in enumerate(concepts)}
    locators = "\n".join(
        f'    <link:loc xlink:type="locator" xlink:href="x.xsd#{concept}" xlink:label="{label}"/>'
        for concept, label in labels.items()
    )
    if repeat is not None:
        locators += (
            f'\n    <link:loc xlink:type="locator" xlink:href="x.xsd#{repeat}" xlink:label="dup"/>'
        )
    edges = "\n".join(
        f'    <link:presentationArc xlink:type="arc" xlink:from="{labels[parent]}"'
        f' xlink:to="{labels[child]}" order="{order}"/>'
        for parent, child, order in (arcs or [])
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<link:linkbase
    xmlns:link="http://www.xbrl.org/2003/linkbase"
    xmlns:xlink="http://www.w3.org/1999/xlink">
  <link:presentationLink xlink:type="extended" xlink:role="{role}">
{locators}
{edges}
  </link:presentationLink>
</link:linkbase>
"""


def _label_link(labels: dict[str, str]) -> str:
    locators = []
    arcs = []
    texts = []
    for index, (concept, text) in enumerate(labels.items()):
        locators.append(
            f'    <link:loc xlink:type="locator" xlink:href="x.xsd#{concept}" xlink:label="c{index}"/>'
        )
        arcs.append(
            f'    <link:labelArc xlink:type="arc" xlink:from="c{index}" xlink:to="t{index}"/>'
        )
        texts.append(
            f'    <link:label xlink:type="resource" xlink:label="t{index}">{text}</link:label>'
        )
    body = "\n".join(locators + arcs + texts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<link:linkbase
    xmlns:link="http://www.xbrl.org/2003/linkbase"
    xmlns:xlink="http://www.w3.org/1999/xlink">
  <link:labelLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">
{body}
  </link:labelLink>
</link:linkbase>
"""


def _write_zip(
    tmp_path: Path, instance: str, presentation: str, label_link: str | None = None
) -> Path:
    import zipfile

    path = tmp_path / "S100TEST.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E00000_2026-03-31_01.xbrl", instance)
        archive.writestr(
            "XBRL/PublicDoc/jpcrp030000-asr-001_E00000_2026-03-31_01_pre.xml", presentation
        )
        if label_link is not None:
            archive.writestr(
                "XBRL/PublicDoc/jpcrp030000-asr-001_E00000_2026-03-31_01_lab.xml", label_link
            )
        # 監査報告書は本文ではない。ここの XBRL を拾ってしまわないことを確かめる。
        archive.writestr("XBRL/AuditDoc/jpaud-aai-cc-001_E00000_2026-03-31_01.xbrl", instance)
    return path


@pytest.fixture
def segment_document(tmp_path: Path) -> Path:
    """連結全体とセグメント別の営業利益が並ぶ書類."""
    contexts = "".join(
        [
            _context("c", start="2025-04-01", end="2026-03-31"),
            _context("CurrentYearDuration", start="2025-04-01", end="2026-03-31"),
            _context(
                "CurrentYearDuration_ReportableSegmentsMember", start="2025-04-01", end="2026-03-31"
            ),
            _context("Prior1YearDuration", start="2024-04-01", end="2025-03-31"),
        ]
    )
    facts = """
        <jppfs_cor:OperatingIncome contextRef="CurrentYearDuration" unitRef="JPY" decimals="-6">1916000000</jppfs_cor:OperatingIncome>
        <jppfs_cor:OperatingIncome contextRef="CurrentYearDuration_ReportableSegmentsMember" unitRef="JPY" decimals="-6">3864000000</jppfs_cor:OperatingIncome>
        <jppfs_cor:OperatingIncome contextRef="Prior1YearDuration" unitRef="JPY" decimals="-6">2318000000</jppfs_cor:OperatingIncome>
    """
    return _write_zip(
        tmp_path,
        _instance(contexts, facts),
        _presentation(_PL_ROLE, ["jppfs_cor_OperatingIncome"]),
    )


def test_連結全体とセグメント別が別の行になる(segment_document: Path) -> None:
    """同じ勘定・同じ期間でも context が違えば別の値。潰すと後から区別できない."""
    facts = parse_document(segment_document).facts

    current = {
        fact.context_ref: fact.value for fact in facts if fact.period_end == date(2026, 3, 31)
    }
    assert current == {
        "CurrentYearDuration": Decimal("1916000000"),
        "CurrentYearDuration_ReportableSegmentsMember": Decimal("3864000000"),
    }


def test_連結全体の値はmemberが空になる(segment_document: Path) -> None:
    """``member IS NULL`` で連結全体だけを引けるようにする."""
    facts = {fact.context_ref: fact.member for fact in parse_document(segment_document).facts}

    assert facts["CurrentYearDuration"] is None
    assert facts["CurrentYearDuration_ReportableSegmentsMember"] == "ReportableSegmentsMember"


def test_期間の種別と日付を持つ(segment_document: Path) -> None:
    fact = next(
        f for f in parse_document(segment_document).facts if f.context_ref == "CurrentYearDuration"
    )

    assert fact.period_type == "duration"
    assert fact.period_start == date(2025, 4, 1)
    assert fact.period_end == date(2026, 3, 31)
    assert fact.unit == "JPY"
    assert fact.decimals == "-6"


def test_同じ勘定が表示リンクに2度出ても1行にまとまる(tmp_path: Path) -> None:
    """キャッシュ・フロー計算書で同じ勘定が複数の小計にぶら下がる。合算すると二重に数える."""
    contexts = _context("c", start="2025-04-01", end="2026-03-31") + _context(
        "CurrentYearDuration", start="2025-04-01", end="2026-03-31"
    )
    facts = """
        <jppfs_cor:OperatingIncome contextRef="CurrentYearDuration" unitRef="JPY">100</jppfs_cor:OperatingIncome>
    """
    path = _write_zip(
        tmp_path,
        _instance(contexts, facts),
        _presentation(_PL_ROLE, ["jppfs_cor_OperatingIncome"], repeat="jppfs_cor_OperatingIncome"),
    )

    assert len(parse_document(path).facts) == 1


def test_値の無い要素は落とす(tmp_path: Path) -> None:
    """nil は「開示はあるが数値が無い」。分析に使えないので行にしない."""
    contexts = _context("c", start="2025-04-01", end="2026-03-31") + _context(
        "CurrentYearDuration", start="2025-04-01", end="2026-03-31"
    )
    facts = """
        <jppfs_cor:OperatingIncome contextRef="CurrentYearDuration" xsi:nil="true"/>
    """
    path = _write_zip(
        tmp_path,
        _instance(contexts, facts),
        _presentation(_PL_ROLE, ["jppfs_cor_OperatingIncome"]),
    )

    assert parse_document(path).facts == ()


def test_証券コードは5桁のまま返す(segment_document: Path) -> None:
    """末尾の 0 を落とすのは呼び出し側の仕事。7550 を rstrip すると 755 になる."""
    assert parse_document(segment_document).info.sec_code == "75500"


def test_書類の素性を読む(segment_document: Path) -> None:
    info = parse_document(segment_document).info

    assert info.filer_name == "試験株式会社"
    assert info.accounting_standard == "Japan GAAP"
    assert info.is_consolidated is True
    assert info.fiscal_year_end == date(2026, 3, 31)


def test_単体決算の会社は単体の役割から読む(tmp_path: Path) -> None:
    """連結を作らない会社は rol_StatementOfIncome しか持たない."""
    dei = """
        <jpdei_cor:SecurityCodeDEI contextRef="c">13010</jpdei_cor:SecurityCodeDEI>
        <jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI contextRef="c">false</jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI>
    """
    contexts = _context("c", start="2025-04-01", end="2026-03-31") + _context(
        "CurrentYearDuration_NonConsolidatedMember", start="2025-04-01", end="2026-03-31"
    )
    facts = """
        <jppfs_cor:OperatingIncome contextRef="CurrentYearDuration_NonConsolidatedMember" unitRef="JPY">42</jppfs_cor:OperatingIncome>
    """
    path = _write_zip(
        tmp_path,
        _instance(contexts, facts, dei=dei),
        _presentation(_PL_STANDALONE_ROLE, ["jppfs_cor_OperatingIncome"]),
    )

    parsed = parse_document(path)
    assert parsed.info.is_consolidated is False
    assert [(f.section, f.value) for f in parsed.facts] == [("PL", Decimal("42"))]


def test_書類に同梱されたラベルを返す(tmp_path: Path) -> None:
    contexts = _context("c", start="2025-04-01", end="2026-03-31")
    path = _write_zip(
        tmp_path,
        _instance(contexts, ""),
        _presentation(_PL_ROLE, ["jppfs_cor_OperatingIncome"]),
        _label_link({"jppfs_cor_OperatingIncome": "営業利益"}),
    )

    assert parse_document(path).labels == {"jppfs_cor_OperatingIncome": "営業利益"}


def test_本文のXBRLが無ければ例外(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "S100EMPTY.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRL/AuditDoc/jpaud.xbrl", "<a/>")

    with pytest.raises(EdinetXbrlError, match="本文の XBRL"):
        parse_document(path)


def test_DEIが無ければ例外(tmp_path: Path) -> None:
    """DEI が読めないと連結か単体かが決まらない。黙って空の結果を返さない."""
    instance = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>
"""
    path = _write_zip(tmp_path, instance, _presentation(_PL_ROLE, ["jppfs_cor_OperatingIncome"]))

    with pytest.raises(EdinetXbrlError, match="DEI"):
        parse_document(path)


def _statement(tmp_path: Path, repeat: str | None = None) -> Path:
    """損益計算書らしい階層を持つ書類を作る.

    Statement (根) → LineItems → 売上高 / 売上原価 / 売上総利益 の並び。
    """
    contexts = _context("c", start="2025-04-01", end="2026-03-31") + _context(
        "CurrentYearDuration", start="2025-04-01", end="2026-03-31"
    )
    facts = """
        <jppfs_cor:NetSales contextRef="CurrentYearDuration" unitRef="JPY">300</jppfs_cor:NetSales>
        <jppfs_cor:CostOfSales contextRef="CurrentYearDuration" unitRef="JPY">100</jppfs_cor:CostOfSales>
        <jppfs_cor:GrossProfit contextRef="CurrentYearDuration" unitRef="JPY">200</jppfs_cor:GrossProfit>
    """
    concepts = [
        "jppfs_cor_StatementOfIncomeHeading",
        "jppfs_cor_StatementOfIncomeLineItems",
        "jppfs_cor_NetSales",
        "jppfs_cor_CostOfSales",
        "jppfs_cor_GrossProfit",
    ]
    # order は刷られる順。わざと宣言の順と食い違わせて、order が効くことを確かめる
    arcs = [
        ("jppfs_cor_StatementOfIncomeHeading", "jppfs_cor_StatementOfIncomeLineItems", 1.0),
        ("jppfs_cor_StatementOfIncomeLineItems", "jppfs_cor_GrossProfit", 3.0),
        ("jppfs_cor_StatementOfIncomeLineItems", "jppfs_cor_NetSales", 1.0),
        ("jppfs_cor_StatementOfIncomeLineItems", "jppfs_cor_CostOfSales", 2.0),
    ]
    return _write_zip(
        tmp_path,
        _instance(contexts, facts),
        _presentation(_PL_ROLE, concepts, repeat=repeat, arcs=arcs),
    )


def test_計算書に刷られる順に並ぶ(tmp_path: Path) -> None:
    """並びは表示リンクの order で決まる。宣言の順ではない."""
    facts = sorted(parse_document(_statement(tmp_path)).facts, key=lambda f: f.ordinal)

    assert [f.concept for f in facts] == [
        "jppfs_cor_NetSales",
        "jppfs_cor_CostOfSales",
        "jppfs_cor_GrossProfit",
    ]


def test_階層の深さを持つ(tmp_path: Path) -> None:
    """Heading が 0、LineItems が 1、勘定が 2 になる."""
    facts = parse_document(_statement(tmp_path)).facts

    assert {f.depth for f in facts} == {2}


def test_同じ勘定に複数のlocatorがあっても木が切れない(tmp_path: Path) -> None:
    """有報の表示リンクでは Table と LineItems に locator が 2 つ振られている.

    locator を節点にすると根が分裂し、途中までしか辿れない。勘定を節点にすれば繋がる。
    """
    facts = parse_document(
        _statement(tmp_path, repeat="jppfs_cor_StatementOfIncomeLineItems")
    ).facts

    assert len(facts) == 3
