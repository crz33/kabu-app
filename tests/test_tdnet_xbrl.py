"""決算短信の iXBRL パーサのテスト.

実物の断片をそのまま埋める。iXBRL は HTML に値が埋まっていて、context の定義が
別ファイルにあるといった癖が多い。作り物の XML で通しても意味が薄い。
"""

import zipfile
from dataclasses import replace
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from kabu_app.collectors.tdnet import TdnetDisclosureMeta
from kabu_app.models import TdnetDisclosure, TdnetStatementFact, TdnetSummaryFact
from kabu_app.parsers.tdnet_xbrl import (
    TdnetXbrlError,
    _split_summary_context,
    parse_disclosure,
)
from kabu_app.stores.tdnet import load_disclosures
from kabu_app.stores.tdnet import mark_downloaded as mark_tdnet_downloaded
from kabu_app.stores.tdnet_fact import (
    mark_no_xbrl,
    mark_parsed,
    save_statement_facts,
    save_summary_facts,
    unparsed_disclosures,
)

_HEAD = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
 xmlns:xbrli="http://www.xbrl.org/2003/instance"><body><div style="display:none">
<ix:header><ix:resources>
<xbrli:context id="CurrentYearDuration_ConsolidatedMember_ResultMember"><xbrli:period>
<xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate>
</xbrli:period></xbrli:context>
<xbrli:context id="NextYearDuration_ConsolidatedMember_ForecastMember"><xbrli:period>
<xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2027-03-31</xbrli:endDate>
</xbrli:period></xbrli:context>
<xbrli:context id="CurrentYearInstant"><xbrli:period>
<xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="CurrentYearDuration"><xbrli:period>
<xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate>
</xbrli:period></xbrli:context>
<xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
<xbrli:unit id="Pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
</ix:resources></ix:header></div>
"""

_TAIL = "</body></html>"


def _summary(body: str) -> bytes:
    return (_HEAD + body + _TAIL).encode("utf-8")


_SUMMARY_BODY = """
<ix:nonNumeric name="tse-ed-t:DocumentName">第１四半期決算短信〔日本基準〕（連結）</ix:nonNumeric>
<ix:nonNumeric name="tse-ed-t:FiscalYearEnd">2026-03-31</ix:nonNumeric>
<ix:nonNumeric name="tse-ed-t:SecuritiesCode">43340</ix:nonNumeric>
<ix:nonFraction name="tse-ed-t:QuarterlyPeriod" contextRef="CurrentYearInstant"
 unitRef="Pure" scale="0">1</ix:nonFraction>
<ix:nonFraction name="tse-ed-t:NetSales"
 contextRef="CurrentYearDuration_ConsolidatedMember_ResultMember"
 unitRef="JPY" scale="6" decimals="-6">138,877</ix:nonFraction>
<ix:nonFraction name="tse-ed-t:NetSales"
 contextRef="NextYearDuration_ConsolidatedMember_ForecastMember"
 unitRef="JPY" scale="6" decimals="-6">145,000</ix:nonFraction>
<ix:nonFraction name="tse-ed-t:OperatingIncome"
 contextRef="CurrentYearDuration_ConsolidatedMember_ResultMember"
 unitRef="JPY" scale="6" sign="-">1,149</ix:nonFraction>
"""

_STATEMENT_BODY = """
<ix:nonFraction name="jppfs_cor:NetSales" contextRef="CurrentYearDuration"
 unitRef="JPY" scale="0" decimals="0">138,877,139</ix:nonFraction>
<ix:nonFraction name="jppfs_cor:OperatingIncome" contextRef="CurrentYearDuration"
 unitRef="JPY" scale="0">7,160,735</ix:nonFraction>
"""

_BS_BODY = """
<ix:nonFraction name="jppfs_cor:Assets" contextRef="CurrentYearInstant"
 unitRef="JPY" scale="0">157,202,000</ix:nonFraction>
"""


@pytest.fixture
def zip_path(tmp_path: Path) -> Path:
    """表紙 1 本と添付 2 本を持つ ZIP を作る.

    context の定義は貸借対照表の側だけに置く。実物がそうなっており、損益計算書からは
    参照するだけになる。
    """
    path = tmp_path / "140120260820523427.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "XBRLData/Summary/tse-acedjpsm-89280-20260820389280-ixbrl.htm",
            _summary(_SUMMARY_BODY),
        )
        archive.writestr(
            "XBRLData/Attachment/0101010-acbs01-tse-acedjpfr-89280-2026-06-30-ixbrl.htm",
            _summary(_BS_BODY),
        )
        # 損益計算書には context を置かない。定義は貸借対照表の側にある
        archive.writestr(
            "XBRLData/Attachment/0102010-acpl01-tse-acedjpfr-89280-2026-06-30-ixbrl.htm",
            (
                '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
                + _STATEMENT_BODY
                + "</body></html>"
            ).encode(),
        )
        archive.writestr("XBRLData/Attachment/qualitative.htm", b"<html><body></body></html>")
    return path


def test_表紙から書類の素性を読む(zip_path: Path) -> None:
    info = parse_disclosure(zip_path).info

    assert info.accounting_standard == "Japan GAAP"
    assert info.is_consolidated is True
    assert info.fiscal_year_end == date(2026, 3, 31)
    assert info.quarter == "Q1"
    assert info.sec_code == "43340"


def test_表紙の実績と会社予想を読み分ける(zip_path: Path) -> None:
    facts = parse_disclosure(zip_path).summary_facts
    sales = {f.fact_type: f for f in facts if f.concept == "tse-ed-t_NetSales"}

    assert sales["Result"].value == Decimal("138877000000")
    assert sales["Result"].scope == "Current"
    assert sales["Forecast"].value == Decimal("145000000000")
    assert sales["Forecast"].scope == "Next"
    assert sales["Forecast"].period_end == date(2027, 3, 31)


def test_scaleを掛けて円に直す(zip_path: Path) -> None:
    """表紙は百万円単位で刷られる. 掛けないと桁が 6 つずれる."""
    facts = parse_disclosure(zip_path).summary_facts
    result = next(f for f in facts if f.concept == "tse-ed-t_NetSales" and f.fact_type == "Result")

    assert result.value == Decimal("138877") * 10**6


def test_signが負なら符号を反転する(zip_path: Path) -> None:
    """表に △ で刷られる値に付く. 落とすと赤字が黒字になる."""
    facts = parse_disclosure(zip_path).summary_facts
    operating = next(f for f in facts if f.concept == "tse-ed-t_OperatingIncome")

    assert operating.value == Decimal("-1149000000")


def test_添付は円単位で丸めが無い(zip_path: Path) -> None:
    """同じ売上高が表紙で 138,877 百万円、添付で 138,877,139 円になる."""
    parsed = parse_disclosure(zip_path)
    summary = next(
        f for f in parsed.summary_facts if f.concept == "tse-ed-t_NetSales" and f.scope == "Current"
    )
    statement = next(f for f in parsed.statement_facts if f.concept == "jppfs_cor_NetSales")

    assert summary.value == Decimal("138877000000")
    assert statement.value == Decimal("138877139")


def test_contextが別ファイルにあっても読める(zip_path: Path) -> None:
    """添付は 6 本前後で 1 つのインスタンスを組み、定義は先頭の 1 本にしか無い.

    1 本ずつ独立に読むと、2 本目から期間が引けずファクトが 1 つも取れなくなる。
    """
    facts = parse_disclosure(zip_path).statement_facts
    sections = {f.section for f in facts}

    assert sections == {"BS", "PL"}
    assert next(f for f in facts if f.section == "PL").period_end == date(2026, 3, 31)


def test_計算書と連結区分をファイル名から読む(zip_path: Path) -> None:
    facts = parse_disclosure(zip_path).statement_facts
    pl = next(f for f in facts if f.section == "PL")

    assert pl.term == "a"
    assert pl.is_consolidated is True


def test_ordinalは書類を通した番号になる(zip_path: Path) -> None:
    """計算書ごとにリセットすると主キーがぶつかる.

    株主資本等変動計算書は同じ勘定が表のセルとして何度も出るため、concept と context_ref
    の組では一意にならない。
    """
    facts = parse_disclosure(zip_path).statement_facts

    assert [f.ordinal for f in facts] == list(range(1, len(facts) + 1))
    assert len({f.ordinal for f in facts}) == len(facts)


def test_定性情報は読まない(zip_path: Path) -> None:
    """qualitative.htm は計算書ではない."""
    facts = parse_disclosure(zip_path).statement_facts

    assert all(f.section in {"BS", "PL"} for f in facts)


def test_iXBRLが1本も無い書類は落とす(tmp_path: Path) -> None:
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRLData/Attachment/qualitative.htm", b"<html></html>")

    with pytest.raises(TdnetXbrlError, match="iXBRL が 1 本も"):
        parse_disclosure(path)


def test_表紙が無くても添付だけ読む(tmp_path: Path) -> None:
    """決算期を変えた会社の「第５四半期決算短信」などに表紙が入らない.

    実測 400 件で 2 件あった。添付には財務諸表があるので捨てない。
    """
    path = tmp_path / "no-summary.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "XBRLData/Attachment/0101010-acbs01-tse-acedjpfr-89280-2026-06-30-ixbrl.htm",
            _summary(_BS_BODY),
        )

    parsed = parse_disclosure(path)

    assert parsed.info.accounting_standard is None
    assert parsed.summary_facts == ()
    assert [f.concept for f in parsed.statement_facts] == ["jppfs_cor_Assets"]


def test_予想レンジの上下限を軸に割る() -> None:
    """findocgen は Result と Forecast しか見ておらず、レンジを捨てていた."""
    axes = _split_summary_context("NextYearDuration_ConsolidatedMember_UpperMember")

    assert axes == ("Next", "Year", None, True, "Upper")


def test_配当の内訳は四半期のmemberが付く() -> None:
    axes = _split_summary_context(
        "CurrentYearDuration_YearEndMember_NonConsolidatedMember_ResultMember"
    )

    assert axes == ("Current", "Year", "YearEnd", False, "Result")


def test_四半期累計のcontextを割る() -> None:
    axes = _split_summary_context("CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember")

    assert axes == ("Current", "AccumulatedQ2", None, True, "Result")


def test_区分の無いcontextも通す() -> None:
    """CurrentYearInstant のように Member が 1 つも付かない context がある."""
    axes = _split_summary_context("CurrentYearInstant")

    assert axes == ("Current", "Year", None, None, None)


def test_形の合わないcontextは捨てる() -> None:
    assert _split_summary_context("SomethingElse") is None


def test_解析した結果を書き込む(session: Session, zip_path: Path) -> None:
    _downloaded_disclosure(session)
    parsed = parse_disclosure(zip_path)

    summary = save_summary_facts(session, _DOC_ID, parsed.summary_facts)
    statement = save_statement_facts(session, _DOC_ID, parsed.statement_facts)
    mark_parsed(session, _DOC_ID, info=parsed.info)
    session.flush()

    assert summary == len(parsed.summary_facts)
    assert statement == len(parsed.statement_facts)

    disclosure = session.get(TdnetDisclosure, _DOC_ID)
    assert disclosure is not None
    assert disclosure.accounting_standard == "Japan GAAP"
    assert disclosure.quarter == "Q1"
    assert disclosure.parsed_at is not None


def test_同じ開示を2回解析しても重複しない(session: Session, zip_path: Path) -> None:
    _downloaded_disclosure(session)
    parsed = parse_disclosure(zip_path)

    for _ in range(2):
        save_summary_facts(session, _DOC_ID, parsed.summary_facts)
        save_statement_facts(session, _DOC_ID, parsed.statement_facts)
    session.flush()

    count = session.execute(
        select(func.count()).select_from(TdnetSummaryFact).where(TdnetSummaryFact.doc_id == _DOC_ID)
    ).scalar_one()
    assert count == len(parsed.summary_facts)


def test_xbrl_file列がNULLでも解析対象にする(session: Session) -> None:
    """findocgen から移した 25,702 件はこの列が NULL のままになる.

    XBRL が無いのではなく有無が分からないだけで、実体は全件そろっている。ここで絞ると
    過去分がまるごと対象から外れる。
    """
    load_disclosures(session, [replace(_META, xbrl_file=None)])
    mark_tdnet_downloaded(session, _DOC_ID)
    session.flush()

    assert [d.doc_id for d in unparsed_disclosures(session)] == [_DOC_ID]


def test_XBRLの無い短信は解析済みとして片付ける(session: Session) -> None:
    """決算短信の 1 割ほどが PDF だけで、数値はどうやっても取れない.

    失敗にすると次の実行が毎回拾い直す。解析済みにして、理由だけ残す。
    """
    _downloaded_disclosure(session)
    mark_no_xbrl(session, _DOC_ID)
    session.flush()

    assert unparsed_disclosures(session) == []
    disclosure = session.get(TdnetDisclosure, _DOC_ID)
    assert disclosure is not None
    assert disclosure.parsed_at is not None
    assert disclosure.parse_error is not None


def test_失敗した開示は次の実行が拾い直す(session: Session) -> None:
    _downloaded_disclosure(session)
    mark_parsed(session, _DOC_ID, error="TdnetXbrlError: 表紙の iXBRL が入っていません")
    session.flush()

    assert [d.doc_id for d in unparsed_disclosures(session)] == [_DOC_ID]
    disclosure = session.get(TdnetDisclosure, _DOC_ID)
    assert disclosure is not None
    assert disclosure.parsed_at is None
    assert disclosure.parse_error is not None


def test_開示を消すとファクトも消える(session: Session, zip_path: Path) -> None:
    _downloaded_disclosure(session)
    parsed = parse_disclosure(zip_path)
    save_summary_facts(session, _DOC_ID, parsed.summary_facts)
    save_statement_facts(session, _DOC_ID, parsed.statement_facts)
    session.flush()

    session.execute(delete(TdnetDisclosure).where(TdnetDisclosure.doc_id == _DOC_ID))
    session.flush()

    remaining = session.execute(select(func.count()).select_from(TdnetStatementFact)).scalar_one()
    assert remaining == 0


_DOC_ID = "140120260820523427"

_META = TdnetDisclosureMeta(
    doc_id=_DOC_ID,
    disclosed_date=date(2026, 8, 20),
    disclosed_time=time(12, 33),
    sec_code="89280",
    code="8928",
    company_name="株式会社サンプル",
    title="2026年3月期 第1四半期決算短信〔日本基準〕（連結）",
    markets="東",
    is_amendment=False,
    xbrl_file="140120260820523427.zip",
)


def _downloaded_disclosure(session: Session) -> None:
    load_disclosures(session, [_META])
    mark_tdnet_downloaded(session, _DOC_ID)
    session.flush()
