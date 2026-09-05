"""決算短信の財務項目の名寄せのテスト.

項目の定義は有報と共有しているので、要素名の当たり方はそちらのテストが見ている。
ここは短信固有の期の数え方を見る。
"""

from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from kabu_app.collectors.edinet import EdinetDocumentMeta
from kabu_app.collectors.tdnet import TdnetDisclosureMeta
from kabu_app.models import Stock
from kabu_app.normalizers.financials import (
    NET_ASSETS,
    NET_SALES,
    OPERATING_INCOME,
    TOTAL_ASSETS,
)
from kabu_app.normalizers.financials import FinancialValue as EdinetFinancialValue
from kabu_app.normalizers.tdnet_financials import (
    INTERIM,
    QUARTER,
    QUARTER_END,
    YEAR,
    YEAR_END,
    YTD,
    FinancialValue,
    SourceFact,
    normalize,
    period_kind_of,
)
from kabu_app.parsers.edinet_xbrl import DocumentInfo
from kabu_app.stores.edinet import load_documents
from kabu_app.stores.edinet_fact import mark_parsed as mark_edinet_parsed
from kabu_app.stores.edinet_financial import save_financials
from kabu_app.stores.tdnet import load_disclosures
from kabu_app.stores.tdnet_fact import (
    disclosures_to_normalize,
    mark_parsed,
    save_tdnet_financials,
)


def _fact(
    section: str,
    concept: str,
    context_ref: str,
    value: str,
    period_end: date = date(2025, 12, 31),
    period_start: date | None = date(2025, 7, 1),
) -> SourceFact:
    return SourceFact(
        section=section,
        concept=concept,
        context_ref=context_ref,
        period_type="instant" if period_start is None else "duration",
        period_start=period_start,
        period_end=period_end,
        value=Decimal(value),
        unit="JPY",
    )


def test_有報と同じ要素名がそのまま当たる() -> None:
    """添付は jppfs_cor で、項目の定義を書き足す必要が無い."""
    facts = [
        _fact("PL", "jppfs_cor_NetSales", "CurrentYTDDuration", "100"),
        _fact("PL", "jppfs_cor_OperatingIncome", "CurrentYTDDuration", "20"),
        _fact("BS", "jppfs_cor_Assets", "CurrentQuarterInstant", "500", period_start=None),
        _fact("BS", "jppfs_cor_NetAssets", "CurrentQuarterInstant", "300", period_start=None),
    ]

    values = normalize(facts)

    assert {v.item: v.value for v in values} == {
        NET_SALES: Decimal("100"),
        OPERATING_INCOME: Decimal("20"),
        TOTAL_ASSETS: Decimal("500"),
        NET_ASSETS: Decimal("300"),
    }


def test_累計と単独四半期を別の行にする() -> None:
    """同じ期末に両方が並ぶ. 取り違えると Q2 累計 100 億が単独 50 億になる."""
    facts = [
        _fact("PL", "jppfs_cor_NetSales", "CurrentYTDDuration", "100"),
        _fact(
            "PL",
            "jppfs_cor_NetSales",
            "CurrentQuarterDuration",
            "50",
            period_start=date(2025, 10, 1),
        ),
    ]

    values = normalize(facts)

    assert {(v.period_kind, v.value) for v in values} == {
        (YTD, Decimal("100")),
        (QUARTER, Decimal("50")),
    }


def test_前年同期は期末の日付で分かれる() -> None:
    facts = [
        _fact("PL", "jppfs_cor_NetSales", "CurrentYTDDuration", "100"),
        _fact(
            "PL",
            "jppfs_cor_NetSales",
            "Prior1YTDDuration",
            "90",
            period_end=date(2024, 12, 31),
            period_start=date(2024, 7, 1),
        ),
    ]

    values = normalize(facts)

    assert [(v.period_end.year, v.value) for v in values] == [
        (2024, Decimal("90")),
        (2025, Decimal("100")),
    ]
    assert {v.period_kind for v in values} == {YTD}


def test_IFRSの要素も寄せる() -> None:
    facts = [
        _fact("PL", "jpigp_cor_RevenueIFRS", "CurrentYTDDuration", "100"),
        _fact("BS", "jpigp_cor_AssetsIFRS", "CurrentQuarterInstant", "500", period_start=None),
    ]

    values = normalize(facts)

    assert {v.item for v in values} == {NET_SALES, TOTAL_ASSETS}


def test_期間の種別が合わない値は拾わない() -> None:
    """総資産は時点の値. 期間として入っているものは別物になる."""
    values = normalize([_fact("BS", "jppfs_cor_Assets", "CurrentYearDuration", "500")])

    assert values == []


def test_提出日時点の値は捨てる() -> None:
    """FilingDateInstant には株式数などが入る. 財務諸表の項目ではない."""
    values = normalize(
        [_fact("BS", "jppfs_cor_Assets", "FilingDateInstant", "500", period_start=None)]
    )

    assert values == []


def test_契約の合わないcontextは捨てる() -> None:
    assert period_kind_of("SomethingElse") is None
    assert period_kind_of("FilingDateInstant") is None


def test_contextから期の種類を読む() -> None:
    assert period_kind_of("CurrentYTDDuration") == YTD
    assert period_kind_of("Prior1YTDDuration") == YTD
    assert period_kind_of("CurrentQuarterDuration") == QUARTER
    assert period_kind_of("CurrentYearDuration") == YEAR
    assert period_kind_of("InterimDuration") == INTERIM
    assert period_kind_of("CurrentQuarterInstant") == QUARTER_END
    assert period_kind_of("Prior1YearInstant") == YEAR_END
    assert period_kind_of("Prior2YearInstant") == YEAR_END


def test_出所を残す() -> None:
    values = normalize([_fact("PL", "jppfs_cor_NetSales", "CurrentYTDDuration", "100")])

    assert values[0].source_section == "PL"
    assert values[0].source_concept == "jppfs_cor_NetSales"


def test_単体決算のcontextからも期の種類を読む() -> None:
    """単体決算の会社は context の後ろに member が付く.

    末尾で合わせると、この会社の数値が丸ごと落ちる。実測 763 書類が該当した。
    """
    assert period_kind_of("CurrentQuarterInstant_NonConsolidatedMember") == QUARTER_END
    assert period_kind_of("Prior1YTDDuration_NonConsolidatedMember") == YTD
    assert period_kind_of("CurrentYearDuration_NonConsolidatedMember") == YEAR


def test_単体決算の書類も名寄せできる() -> None:
    facts = [
        _fact(
            "PL",
            "jppfs_cor_NetSales",
            "CurrentYTDDuration_NonConsolidatedMember",
            "100",
        ),
        _fact(
            "BS",
            "jppfs_cor_Assets",
            "CurrentQuarterInstant_NonConsolidatedMember",
            "500",
            period_start=None,
        ),
    ]

    values = normalize(facts)

    assert {(v.item, v.period_kind) for v in values} == {
        (NET_SALES, YTD),
        (TOTAL_ASSETS, QUARTER_END),
    }


def test_有報と短信が1枚に並ぶ(session: Session) -> None:
    """financials は報告のぜんぶを縦に並べる. 同じ期が何行も出る."""
    _edinet_row(session, period_end=date(2025, 3, 31), value=1000, submit_date=date(2025, 6, 20))
    _tdnet_row(session, kind=YTD, period_end=date(2025, 9, 30), value=600)
    session.flush()

    rows = session.execute(
        text(
            "SELECT period_kind, value, source, available_at FROM financials"
            " WHERE code = :code AND item = 'net_sales' ORDER BY period_end"
        ),
        {"code": _CODE},
    ).all()

    assert [(r[0], int(r[1]), r[2]) for r in rows] == [
        ("year", 1000, "edinet"),
        (YTD, 600, "tdnet"),
    ]


def test_同じ期は有報を採る(session: Session) -> None:
    """監査を通った確定値だから. 短信が先に出ていても有報で上書きされる."""
    _edinet_row(session, period_end=date(2025, 3, 31), value=1000, submit_date=date(2025, 6, 20))
    _tdnet_row(session, kind="year", period_end=date(2025, 3, 31), value=999)
    session.flush()

    rows = session.execute(
        text(
            "SELECT source, value FROM latest_financials"
            " WHERE code = :code AND item = 'net_sales' AND period_kind = 'year'"
        ),
        {"code": _CODE},
    ).all()

    assert [(r[0], int(r[1])) for r in rows] == [("edinet", 1000)]


def test_有報がまだ無い期は短信を採る(session: Session) -> None:
    """通期の短信が出てから有報が出るまで 1.5 か月ある."""
    _tdnet_row(session, kind="year", period_end=date(2026, 3, 31), value=1200)
    session.flush()

    rows = session.execute(
        text(
            "SELECT source, value FROM latest_financials"
            " WHERE code = :code AND item = 'net_sales' AND period_kind = 'year'"
        ),
        {"code": _CODE},
    ).all()

    assert [(r[0], int(r[1])) for r in rows] == [("tdnet", 1200)]


def test_時点で絞ると当時の報告が残る(session: Session) -> None:
    """有報は 5 期分を載せるので、同じ期を何通もの書類が報告する.

    期ごとに 1 行へ絞ってしまうと available_at が新しい書類のものになり、
    「2020 年時点で 2019 年の売上が使えない」ことになる。
    """
    _edinet_row(session, period_end=date(2025, 3, 31), value=1000, submit_date=date(2025, 6, 20))
    # 翌年の有報が前期分として同じ期を報告し直す
    _edinet_row(
        session,
        period_end=date(2025, 3, 31),
        value=1000,
        submit_date=date(2026, 6, 20),
        doc_id="S100LATER",
    )
    session.flush()

    at_2025 = session.execute(
        text(
            "SELECT count(*) FROM financials WHERE code = :code AND item = 'net_sales'"
            " AND available_at <= '2025-12-31'"
        ),
        {"code": _CODE},
    ).scalar_one()
    latest = session.execute(
        text("SELECT count(*) FROM latest_financials WHERE code = :code AND item = 'net_sales'"),
        {"code": _CODE},
    ).scalar_one()

    assert at_2025 == 1
    assert latest == 1


_CODE = "9999"


def _edinet_row(
    session: Session,
    period_end: date,
    value: int,
    submit_date: date,
    doc_id: str = "S100TEST0",
) -> None:
    """有報 1 件と、その名寄せ結果 1 行を入れる."""
    load_documents(
        session,
        [
            EdinetDocumentMeta(
                doc_id=doc_id,
                edinet_code="E00000",
                sec_code=f"{_CODE}0",
                code=_CODE,
                doc_type_code="120",
                parent_doc_id=None,
                submit_date=submit_date,
                submitted_at=None,
                period_end=period_end,
                filer_name="テスト株式会社",
                doc_description="有価証券報告書",
                has_xbrl=True,
                is_withdrawn=False,
            )
        ],
    )
    mark_edinet_parsed(
        session,
        doc_id,
        info=DocumentInfo(
            sec_code=f"{_CODE}0",
            filer_name="テスト株式会社",
            accounting_standard="Japan GAAP",
            is_consolidated=True,
            fiscal_year_start=date(period_end.year - 1, period_end.month, 1),
            fiscal_year_end=period_end,
        ),
    )
    save_financials(
        session,
        doc_id,
        [
            EdinetFinancialValue(
                item=NET_SALES,
                period_start=date(period_end.year - 1, 4, 1),
                period_end=period_end,
                value=Decimal(value),
                unit="JPY",
                source_section="BR",
                source_concept="jpcrp_cor_NetSalesSummaryOfBusinessResults",
            )
        ],
    )


def _tdnet_row(session: Session, kind: str, period_end: date, value: int) -> None:
    """短信 1 件と、その名寄せ結果 1 行を入れる."""
    doc_id = f"TD{kind}{period_end:%Y%m%d}"[:24]
    load_disclosures(
        session,
        [
            TdnetDisclosureMeta(
                doc_id=doc_id,
                disclosed_date=date(period_end.year, period_end.month, 1) + timedelta(days=44),
                disclosed_time=time(15, 0),
                sec_code=f"{_CODE}0",
                code=_CODE,
                company_name="テスト株式会社",
                title="決算短信",
                markets="東",
                is_amendment=False,
                xbrl_file=f"{doc_id}.zip",
            )
        ],
    )
    save_tdnet_financials(
        session,
        doc_id,
        [
            FinancialValue(
                item=NET_SALES,
                period_kind=kind,
                period_start=date(period_end.year, 4, 1),
                period_end=period_end,
                value=Decimal(value),
                unit="JPY",
                source_section="PL",
                source_concept="jppfs_cor_NetSales",
            )
        ],
    )


def _stock(code: str, is_listed: bool = True) -> Stock:
    return Stock(
        code=code,
        name=f"銘柄{code}",
        market_segment="prime",
        industry33_code="0050",
        industry33_name="水産・農林業",
        industry17_code="01",
        industry17_name="食品",
        topix_scale_code=None,
        topix_scale_name=None,
        base_date=date(2026, 7, 31),
        is_listed=is_listed,
    )


def _parsed_disclosure(session: Session, code: str, doc_id: str) -> None:
    """名寄せ待ちの短信を 1 件入れる. 解析は済んで名寄せ結果がまだ無い状態."""
    load_disclosures(
        session,
        [
            TdnetDisclosureMeta(
                doc_id=doc_id,
                disclosed_date=date(2026, 8, 14),
                disclosed_time=time(15, 0),
                sec_code=f"{code}0",
                code=code,
                company_name=f"会社{code}",
                title="決算短信",
                markets="東",
                is_amendment=False,
                xbrl_file=f"{doc_id}.zip",
            )
        ],
    )
    mark_parsed(session, doc_id)
    session.flush()


def test_名寄せの対象はstocksに居る銘柄だけ(session: Session) -> None:
    """TDnet は表題で拾うので、REIT や地方市場の単独上場銘柄が混ざる."""
    _parsed_disclosure(session, code="7203", doc_id="TD7203")
    _parsed_disclosure(session, code="8951", doc_id="TD8951")
    session.add(_stock("7203"))
    session.flush()

    assert [d.doc_id for d in disclosures_to_normalize(session)] == ["TD7203"]


def test_上場廃止した銘柄も名寄せする(session: Session) -> None:
    """stocks は廃止後も行を残す。過去の決算に生存者バイアスを入れないため."""
    _parsed_disclosure(session, code="7203", doc_id="TD7203")
    session.add(_stock("7203", is_listed=False))
    session.flush()

    assert [d.doc_id for d in disclosures_to_normalize(session)] == ["TD7203"]
