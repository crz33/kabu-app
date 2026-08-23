"""決算短信の財務項目の名寄せのテスト.

項目の定義は有報と共有しているので、要素名の当たり方はそちらのテストが見ている。
ここは短信固有の期の数え方を見る。
"""

from datetime import date
from decimal import Decimal

from kabu_app.normalizers.financials import (
    NET_ASSETS,
    NET_SALES,
    OPERATING_INCOME,
    TOTAL_ASSETS,
)
from kabu_app.normalizers.tdnet_financials import (
    INTERIM,
    QUARTER,
    QUARTER_END,
    YEAR,
    YEAR_END,
    YTD,
    SourceFact,
    normalize,
    period_kind_of,
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
