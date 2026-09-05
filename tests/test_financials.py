"""財務項目の名寄せのテスト.

名寄せそのものは純粋関数なので DB を使わない。書き込みのテストだけ conftest の
セッションを使う。
"""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kabu_app.collectors.edinet import EdinetDocumentMeta
from kabu_app.models import EdinetFinancial, Stock
from kabu_app.normalizers.financials import (
    NET_ASSETS,
    NET_INCOME,
    NET_SALES,
    OPERATING_INCOME,
    ORDINARY_INCOME,
    TOTAL_ASSETS,
    FinancialValue,
    SourceFact,
    normalize,
)
from kabu_app.parsers.edinet_xbrl import DocumentInfo, Fact
from kabu_app.stores.edinet import load_documents, mark_downloaded
from kabu_app.stores.edinet_fact import (
    mark_parsed,
    save_document_labels,
    save_facts,
    save_labels,
)
from kabu_app.stores.edinet_financial import (
    documents_to_normalize,
    load_source_facts,
    save_financials,
)

_DOC_ID = "S100YW7F"

_META = EdinetDocumentMeta(
    doc_id=_DOC_ID,
    edinet_code="E05729",
    sec_code="21680",
    code="2168",
    doc_type_code="120",
    parent_doc_id=None,
    submit_date=date(2026, 8, 14),
    submitted_at=datetime(2026, 8, 14, 16, 0),
    period_end=date(2026, 5, 31),
    filer_name="株式会社パソナグループ",
    doc_description="有価証券報告書－第49期",
    has_xbrl=True,
    is_withdrawn=False,
)

_INFO = DocumentInfo(
    sec_code="21680",
    filer_name="株式会社パソナグループ",
    accounting_standard="Japan GAAP",
    is_consolidated=True,
    fiscal_year_start=date(2025, 6, 1),
    fiscal_year_end=date(2026, 5, 31),
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


def _parsed_document(session: Session, with_stock: bool = True) -> None:
    if with_stock:
        session.add(_stock(_META.code))
    load_documents(session, [_META])
    mark_downloaded(session, _DOC_ID)
    mark_parsed(session, _DOC_ID, info=_INFO)
    session.flush()


def _stored_fact(concept: str, context_ref: str, value: str, section: str = "BR") -> Fact:
    return Fact(
        section=section,
        concept=concept,
        context_ref=context_ref,
        member=None if "_" not in context_ref else context_ref.split("_", 1)[1],
        ordinal=1,
        depth=2,
        period_type="duration",
        period_start=date(2025, 6, 1),
        period_end=date(2026, 5, 31),
        value=Decimal(value),
        unit="JPY",
        decimals="-6",
    )


def _fact(
    section: str,
    concept: str,
    value: str,
    period_end: date = date(2026, 5, 31),
    period_type: str = "duration",
    label: str | None = None,
) -> SourceFact:
    return SourceFact(
        section=section,
        concept=concept,
        period_type=period_type,
        period_start=date(2025, 6, 1) if period_type == "duration" else None,
        period_end=period_end,
        value=Decimal(value),
        unit="JPY",
        label=label,
    )


def _picked(values: list[FinancialValue], item: str) -> list[tuple[date, Decimal, str]]:
    return [(v.period_end, v.value, v.source_concept) for v in values if v.item == item]


def _six_items() -> list[SourceFact]:
    return [
        _fact("BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults", "100"),
        _fact("PL", "jppfs_cor_OperatingIncome", "20"),
        _fact("BR", "jpcrp_cor_OrdinaryIncomeLossSummaryOfBusinessResults", "18"),
        _fact(
            "BR", "jpcrp_cor_ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults", "9"
        ),
        _fact("BR", "jpcrp_cor_TotalAssetsSummaryOfBusinessResults", "500", period_type="instant"),
        _fact("BR", "jpcrp_cor_NetAssetsSummaryOfBusinessResults", "300", period_type="instant"),
    ]


def test_日本GAAPの6項目を寄せる() -> None:
    values = normalize(_six_items())

    assert {v.item: v.value for v in values} == {
        NET_SALES: Decimal("100"),
        OPERATING_INCOME: Decimal("20"),
        ORDINARY_INCOME: Decimal("18"),
        NET_INCOME: Decimal("9"),
        TOTAL_ASSETS: Decimal("500"),
        NET_ASSETS: Decimal("300"),
    }


def test_IFRSの売上収益を売上高に寄せる() -> None:
    values = normalize([_fact("PL", "jpigp_cor_RevenueIFRS", "100")])

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("100"), "jpigp_cor_RevenueIFRS")
    ]


def test_IFRSには経常利益が無いので税引前利益で代える() -> None:
    values = normalize(
        [_fact("BR", "jpcrp_cor_ProfitLossBeforeTaxIFRSSummaryOfBusinessResults", "18")]
    )

    assert _picked(values, ORDINARY_INCOME) == [
        (
            date(2026, 5, 31),
            Decimal("18"),
            "jpcrp_cor_ProfitLossBeforeTaxIFRSSummaryOfBusinessResults",
        )
    ]


def test_経営指標の推移を損益計算書より優先する() -> None:
    """同じ期に両方あれば BR を採る. 値は一致するが 5 期分そろうほうを基準にする."""
    facts = [
        _fact("PL", "jppfs_cor_NetSales", "100"),
        _fact("BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults", "100"),
    ]

    values = normalize(facts)

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("100"), "jpcrp_cor_NetSalesSummaryOfBusinessResults")
    ]


def test_会計基準を切り替えた期はIFRSを採る() -> None:
    """両方の要素が入る書類がある. 新しい基準のほうを残す."""
    facts = [
        _fact("BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults", "100"),
        _fact("BR", "jpcrp_cor_RevenueIFRSSummaryOfBusinessResults", "105"),
    ]

    values = normalize(facts)

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("105"), "jpcrp_cor_RevenueIFRSSummaryOfBusinessResults")
    ]


def test_経営指標の推移から5期分を取る() -> None:
    facts = [
        _fact(
            "BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults", str(100 + year), date(year, 5, 31)
        )
        for year in range(2022, 2027)
    ]

    values = normalize(facts)

    assert [(v.period_end.year, v.value) for v in values if v.item == NET_SALES] == [
        (2022, Decimal("2122")),
        (2023, Decimal("2123")),
        (2024, Decimal("2124")),
        (2025, Decimal("2125")),
        (2026, Decimal("2126")),
    ]


def test_会社独自の要素は日本語ラベルで拾う() -> None:
    """鉄道や金融の営業収益は要素名が業種ごとに割れる. ラベルで受ける."""
    values = normalize(
        [_fact("PL", "jpcrp030000-asr_E00000-000_OperatingRevenue", "100", label="営業収益")]
    )

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("100"), "jpcrp030000-asr_E00000-000_OperatingRevenue")
    ]


def test_ラベルは読点で続く付加情報を許す() -> None:
    values = normalize(
        [_fact("BR", "jpcrp030000-asr_E00000-000_Sales", "100", label="事業収益、経営指標等")]
    )

    assert len(_picked(values, NET_SALES)) == 1


def test_要素名で取れた期にはラベルを使わない() -> None:
    facts = [
        _fact("BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults", "100"),
        _fact("BR", "jpcrp030000-asr_E00000-000_Other", "999", label="売上高"),
    ]

    values = normalize(facts)

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("100"), "jpcrp_cor_NetSalesSummaryOfBusinessResults")
    ]


def test_同じラベルが複数あれば合計とみて大きいほうを採る() -> None:
    facts = [
        _fact("PL", "jpcrp030000-asr_E00000-000_SalesA", "30", label="売上高"),
        _fact("PL", "jpcrp030000-asr_E00000-000_SalesTotal", "100", label="売上高"),
    ]

    values = normalize(facts)

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("100"), "jpcrp030000-asr_E00000-000_SalesTotal")
    ]


def test_ラベルのフォールバックは売上だけに効く() -> None:
    """他の項目は標準要素で足りる. 誤爆させないため持たせていない."""
    values = normalize(
        [_fact("PL", "jpcrp030000-asr_E00000-000_Op", "20", label="営業利益又は営業損失（△）")]
    )

    assert _picked(values, OPERATING_INCOME) == []


def test_期間の種別が合わない値は拾わない() -> None:
    """総資産は時点の値. 期間として入っているものは別物なので採らない."""
    values = normalize(
        [
            _fact(
                "BR", "jpcrp_cor_TotalAssetsSummaryOfBusinessResults", "500", period_type="duration"
            )
        ]
    )

    assert _picked(values, TOTAL_ASSETS) == []


def test_知らない要素は捨てる() -> None:
    assert normalize([_fact("PL", "jppfs_cor_SellingGeneralAndAdministrativeExpenses", "50")]) == []


def test_営業利益は経営指標の推移から取れない() -> None:
    """タクソノミに営業利益の SummaryOfBusinessResults 要素が無い. PL からしか取れない."""
    facts = [
        _fact("BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults", "100", date(2022, 5, 31)),
        _fact("BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults", "110", date(2026, 5, 31)),
        _fact("PL", "jppfs_cor_OperatingIncome", "20", date(2026, 5, 31)),
    ]

    values = normalize(facts)

    assert len(_picked(values, NET_SALES)) == 2
    assert len(_picked(values, OPERATING_INCOME)) == 1


def test_書類単位で入れ替える(session: Session) -> None:
    """同じ書類を 2 回名寄せしても増えない."""
    _parsed_document(session)

    save_financials(session, _DOC_ID, normalize(_six_items()))
    save_financials(session, _DOC_ID, normalize(_six_items()))
    session.flush()

    count = session.execute(
        select(func.count()).select_from(EdinetFinancial).where(EdinetFinancial.doc_id == _DOC_ID)
    ).scalar_one()
    assert count == 6


def test_出所を残す(session: Session) -> None:
    """値がおかしいとき、どの要素から寄せたかを追えること."""
    _parsed_document(session)
    save_financials(session, _DOC_ID, normalize(_six_items()))
    session.flush()

    row = session.execute(
        select(EdinetFinancial).where(
            EdinetFinancial.doc_id == _DOC_ID, EdinetFinancial.item == NET_SALES
        )
    ).scalar_one()

    assert row.source_section == "BR"
    assert row.source_concept == "jpcrp_cor_NetSalesSummaryOfBusinessResults"


def test_名寄せ済みの書類は対象から外れる(session: Session) -> None:
    _parsed_document(session)

    assert [d.doc_id for d in documents_to_normalize(session)] == [_DOC_ID]

    save_financials(session, _DOC_ID, normalize(_six_items()))
    session.flush()

    assert documents_to_normalize(session) == []
    assert [d.doc_id for d in documents_to_normalize(session, renormalize=True)] == [_DOC_ID]


def test_未解析の書類は対象にしない(session: Session) -> None:
    load_documents(session, [_META])
    mark_downloaded(session, _DOC_ID)
    session.flush()

    assert documents_to_normalize(session) == []


def test_連結全体の値だけを入力にする(session: Session) -> None:
    """セグメント別の値が混ざると同じ期に候補が並んで取り違える."""
    _parsed_document(session)
    save_facts(
        session,
        _DOC_ID,
        [
            _stored_fact(
                "jpcrp_cor_NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "100"
            ),
            _stored_fact(
                "jpcrp_cor_NetSalesSummaryOfBusinessResults",
                "CurrentYearDuration_SegmentAMember",
                "40",
            ),
        ],
    )
    session.flush()

    facts = load_source_facts(session, _DOC_ID, is_consolidated=True)

    assert [f.value for f in facts] == [Decimal("100")]


def test_書類のラベルを標準ラベルより優先する(session: Session) -> None:
    """会社は標準の勘定に独自の言い換えを付ける. その書類での文言で拾う."""
    _parsed_document(session)
    concept = "jpcrp030000-asr_E05729-000_OperatingRevenue"
    save_facts(session, _DOC_ID, [_stored_fact(concept, "CurrentYearDuration", "100")])
    save_labels(session, {concept: "その他の収入"})
    save_document_labels(session, _DOC_ID, {concept: "営業収益"})
    session.flush()

    facts = load_source_facts(session, _DOC_ID, is_consolidated=True)

    assert [f.label for f in facts] == ["営業収益"]
    assert [v.item for v in normalize(facts)] == [NET_SALES]


def test_最新の書類の値を期ごとに1行だけ残す(session: Session) -> None:
    """訂正有報と、過去 4 期分として遡って載った値が同じ期に並ぶ."""
    _parsed_document(session)
    save_financials(
        session,
        _DOC_ID,
        [
            FinancialValue(
                item=NET_SALES,
                period_start=date(2025, 6, 1),
                period_end=date(2026, 5, 31),
                value=Decimal("100"),
                unit="JPY",
                source_section="BR",
                source_concept="jpcrp_cor_NetSalesSummaryOfBusinessResults",
            )
        ],
    )

    amendment = replace(
        _META,
        doc_id="S100AMEND",
        doc_type_code="130",
        parent_doc_id=_DOC_ID,
        period_end=None,
        submit_date=date(2026, 9, 30),
    )
    load_documents(session, [amendment])
    mark_downloaded(session, "S100AMEND")
    mark_parsed(session, "S100AMEND", info=_INFO)
    save_financials(
        session,
        "S100AMEND",
        [
            FinancialValue(
                item=NET_SALES,
                period_start=date(2025, 6, 1),
                period_end=date(2026, 5, 31),
                value=Decimal("120"),
                unit="JPY",
                source_section="BR",
                source_concept="jpcrp_cor_NetSalesSummaryOfBusinessResults",
            )
        ],
    )
    session.flush()

    rows = session.execute(
        text("SELECT doc_id, value FROM edinet_latest_financials WHERE code = :code"),
        {"code": "2168"},
    ).all()

    assert [(r[0], int(r[1])) for r in rows] == [("S100AMEND", 120)]


def test_単体決算の会社はNonConsolidatedMemberを通す(session: Session) -> None:
    """連結財務諸表を作らない会社は、すべての数値にこの member が付く.

    実測では単体決算 765 書類のうち、BR に member 無しの数値を持つものが 1 件も無い。
    連結企業と同じ条件で引くと 1 項目も取れなくなる。
    """
    _parsed_document(session)
    save_facts(
        session,
        _DOC_ID,
        [
            _stored_fact(
                "jpcrp_cor_NetSalesSummaryOfBusinessResults",
                "CurrentYearDuration_NonConsolidatedMember",
                "100",
            )
        ],
    )
    session.flush()

    assert load_source_facts(session, _DOC_ID, is_consolidated=False) != []
    assert load_source_facts(session, _DOC_ID, is_consolidated=True) == []


def test_連結企業は単体の数値を混ぜない(session: Session) -> None:
    """連結企業の BR_C には単体の数値が NonConsolidatedMember 付きで入る."""
    _parsed_document(session)
    save_facts(
        session,
        _DOC_ID,
        [
            _stored_fact(
                "jpcrp_cor_NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "100"
            ),
            _stored_fact(
                "jpcrp_cor_NetSalesSummaryOfBusinessResults",
                "CurrentYearDuration_NonConsolidatedMember",
                "40",
            ),
        ],
    )
    session.flush()

    facts = load_source_facts(session, _DOC_ID, is_consolidated=True)

    assert [f.value for f in facts] == [Decimal("100")]


def test_単体決算でも株主資本の内訳は通さない(session: Session) -> None:
    """NonConsolidatedMember_XxxMember は株主資本等変動計算書の内訳になる."""
    _parsed_document(session)
    save_facts(
        session,
        _DOC_ID,
        [
            _stored_fact(
                "jpcrp_cor_NetSalesSummaryOfBusinessResults",
                "CurrentYearDuration_NonConsolidatedMember_ShareholdersEquityMember",
                "40",
            )
        ],
    )
    session.flush()

    assert load_source_facts(session, _DOC_ID, is_consolidated=False) == []


def test_銀行の経常収益を売上高に寄せる() -> None:
    """要素名は OrdinaryIncome だがラベルは「経常収益」で、銀行・保険の収益になる.

    実測 1,073 件はすべて銀行と金融持株会社だった。要素名の Income に釣られて経常利益に
    寄せると、みずほ FG の 9.0 兆円が利益の列に入る。
    """
    values = normalize([_fact("BR", "jpcrp_cor_OrdinaryIncomeSummaryOfBusinessResults", "100")])

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("100"), "jpcrp_cor_OrdinaryIncomeSummaryOfBusinessResults")
    ]
    assert _picked(values, ORDINARY_INCOME) == []


def test_保険業の経常収益も売上高に寄せる() -> None:
    values = normalize([_fact("PL", "jppfs_cor_OperatingIncomeINS", "100")])

    assert _picked(values, NET_SALES) == [
        (date(2026, 5, 31), Decimal("100"), "jppfs_cor_OperatingIncomeINS")
    ]


def test_修正国際基準の要素も拾う() -> None:
    """JMIS の採用は数社しかないが、要素はタクソノミにある."""
    facts = [
        _fact("BR", "jpcrp_cor_RevenueJMISSummaryOfBusinessResults", "100"),
        _fact("BR", "jpcrp_cor_ProfitLossBeforeTaxJMISSummaryOfBusinessResults", "18"),
        _fact(
            "BR",
            "jpcrp_cor_ProfitLossAttributableToOwnersOfParentJMISSummaryOfBusinessResults",
            "9",
        ),
        _fact(
            "BR", "jpcrp_cor_TotalAssetsJMISSummaryOfBusinessResults", "500", period_type="instant"
        ),
        _fact(
            "BR",
            "jpcrp_cor_EquityAttributableToOwnersOfParentJMISSummaryOfBusinessResults",
            "300",
            period_type="instant",
        ),
    ]

    values = normalize(facts)

    assert {v.item for v in values} == {
        NET_SALES,
        ORDINARY_INCOME,
        NET_INCOME,
        TOTAL_ASSETS,
        NET_ASSETS,
    }


def test_経常利益と経常収益を取り違えない() -> None:
    """銀行は両方を持つ. Loss の有無で別の要素になる."""
    facts = [
        _fact("BR", "jpcrp_cor_OrdinaryIncomeSummaryOfBusinessResults", "9030374"),
        _fact("BR", "jpcrp_cor_OrdinaryIncomeLossSummaryOfBusinessResults", "1168141"),
    ]

    values = normalize(facts)

    assert _picked(values, NET_SALES)[0][1] == Decimal("9030374")
    assert _picked(values, ORDINARY_INCOME)[0][1] == Decimal("1168141")


def test_名寄せの対象はstocksに居る銘柄だけ(session: Session) -> None:
    """投資の対象はプライム・スタンダード・グロースの内国株に限る.

    ここに来るのはほとんどが上場廃止した会社で、株価が取れないので使い道が無い。
    """
    _parsed_document(session, with_stock=False)

    assert documents_to_normalize(session) == []


def test_上場廃止した銘柄も名寄せする(session: Session) -> None:
    """stocks は廃止後も行を残す。廃止直後は直近の決算がまだ意味を持つ."""
    session.add(_stock(_META.code, is_listed=False))
    load_documents(session, [_META])
    mark_downloaded(session, _DOC_ID)
    mark_parsed(session, _DOC_ID, info=_INFO)
    session.flush()

    assert [d.doc_id for d in documents_to_normalize(session)] == [_DOC_ID]
