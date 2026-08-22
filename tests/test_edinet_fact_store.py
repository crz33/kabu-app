"""有報の解析結果の取り込みのテスト.

セッションは conftest のフィクスチャが用意する。テストの最後にロールバックされる。
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kabu_app.collectors.edinet import EdinetDocumentMeta
from kabu_app.models import (
    EdinetDocument,
    EdinetDocumentLabel,
    EdinetFact,
    EdinetLabel,
    EdinetShareholder,
)
from kabu_app.parsers.edinet_xbrl import Fact
from kabu_app.parsers.shareholders import Shareholder
from kabu_app.stores.edinet import load_documents, mark_downloaded
from kabu_app.stores.edinet_fact import (
    mark_parsed,
    save_document_labels,
    save_facts,
    save_labels,
    save_shareholders,
    unparsed_documents,
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


def _fact(concept: str, context_ref: str, value: str, section: str = "PL") -> Fact:
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


def _downloaded_document(session: Session) -> None:
    load_documents(session, [_META])
    mark_downloaded(session, _DOC_ID)
    session.flush()


def test_未解析の書類を返す(session: Session) -> None:
    _downloaded_document(session)

    assert [d.doc_id for d in unparsed_documents(session)] == [_DOC_ID]


def test_ZIPが未取得の書類は解析対象にしない(session: Session) -> None:
    """本文が無いものを解析しに行っても仕方がない."""
    load_documents(session, [_META])
    session.flush()

    assert unparsed_documents(session) == []


def test_訂正有報は解析対象にしない(session: Session) -> None:
    """様式が有報と違い、同じ手順では財務諸表を取り出せない."""
    from dataclasses import replace

    amendment = replace(_META, doc_id="S100AMEND", doc_type_code="130", parent_doc_id=_DOC_ID)
    load_documents(session, [amendment])
    mark_downloaded(session, "S100AMEND")
    session.flush()

    assert unparsed_documents(session) == []


def test_解析済みは対象から外れる(session: Session) -> None:
    _downloaded_document(session)
    mark_parsed(session, _DOC_ID)
    session.flush()

    assert unparsed_documents(session) == []


def test_再解析を指定すれば解析済みも返す(session: Session) -> None:
    _downloaded_document(session)
    mark_parsed(session, _DOC_ID)
    session.flush()

    assert [d.doc_id for d in unparsed_documents(session, include_parsed=True)] == [_DOC_ID]


def test_失敗した書類は次の実行が拾い直す(session: Session) -> None:
    """parsed_at を埋めない。理由だけ残す."""
    _downloaded_document(session)
    mark_parsed(session, _DOC_ID, error="EdinetXbrlError: DEI が見つかりません")
    session.flush()

    document = session.get(EdinetDocument, _DOC_ID)
    assert document is not None
    assert document.parsed_at is None
    assert document.parse_error is not None
    assert [d.doc_id for d in unparsed_documents(session)] == [_DOC_ID]


def test_解析に成功したら前回の失敗の記録が消える(session: Session) -> None:
    _downloaded_document(session)
    mark_parsed(session, _DOC_ID, error="壊れていた")
    mark_parsed(session, _DOC_ID)
    session.flush()

    document = session.get(EdinetDocument, _DOC_ID)
    assert document is not None
    assert document.parsed_at is not None
    assert document.parse_error is None


def test_ファクトを取り込む(session: Session) -> None:
    _downloaded_document(session)

    saved = save_facts(
        session,
        _DOC_ID,
        [
            _fact("jppfs_cor_OperatingIncome", "CurrentYearDuration", "1916000000"),
            _fact(
                "jppfs_cor_OperatingIncome",
                "CurrentYearDuration_ReportableSegmentsMember",
                "3864000000",
            ),
        ],
    )

    assert saved == 2
    rows = session.execute(select(EdinetFact).order_by(EdinetFact.context_ref)).scalars().all()
    assert [row.value for row in rows] == [Decimal("1916000000"), Decimal("3864000000")]
    assert rows[0].member is None
    assert rows[1].member == "ReportableSegmentsMember"


def test_同じ書類を2回解析しても重複しない(session: Session) -> None:
    """書類単位で消してから入れ直す。バッチを 2 回走らせても壊れない."""
    _downloaded_document(session)
    facts = [_fact("jppfs_cor_OperatingIncome", "CurrentYearDuration", "100")]

    save_facts(session, _DOC_ID, facts)
    save_facts(session, _DOC_ID, facts)

    assert session.execute(select(func.count()).select_from(EdinetFact)).scalar_one() == 1


def test_解析し直すと消えた勘定は残らない(session: Session) -> None:
    _downloaded_document(session)
    save_facts(session, _DOC_ID, [_fact("jppfs_cor_OperatingIncome", "CurrentYearDuration", "1")])

    save_facts(session, _DOC_ID, [_fact("jppfs_cor_NetSales", "CurrentYearDuration", "2")])

    rows = session.execute(select(EdinetFact.concept)).scalars().all()
    assert rows == ["jppfs_cor_NetSales"]


def test_書類を消すとファクトも消える(session: Session) -> None:
    """FK の ON DELETE CASCADE。書類を消して孤児が残らないこと."""
    _downloaded_document(session)
    save_facts(session, _DOC_ID, [_fact("jppfs_cor_OperatingIncome", "CurrentYearDuration", "1")])
    session.flush()

    session.delete(session.get(EdinetDocument, _DOC_ID))
    session.flush()

    assert session.execute(select(func.count()).select_from(EdinetFact)).scalar_one() == 0


def test_標準ラベルを取り込む(session: Session) -> None:
    saved = save_labels(session, {"jppfs_cor_OperatingIncome": "営業利益"})

    assert saved == 1
    label = session.get(EdinetLabel, "jppfs_cor_OperatingIncome")
    assert label is not None
    assert label.label == "営業利益"


def test_書類のラベルは標準ラベルを汚さない(session: Session) -> None:
    """営業利益に「セグメント利益」と書く会社がある。全社に広がると誤表示になる."""
    _downloaded_document(session)
    save_labels(session, {"jppfs_cor_OperatingIncome": "営業利益"})

    save_document_labels(session, _DOC_ID, {"jppfs_cor_OperatingIncome": "セグメント利益"})

    standard = session.get(EdinetLabel, "jppfs_cor_OperatingIncome")
    assert standard is not None
    assert standard.label == "営業利益"
    per_document = session.get(EdinetDocumentLabel, (_DOC_ID, "jppfs_cor_OperatingIncome"))
    assert per_document is not None
    assert per_document.label == "セグメント利益"


def test_書類のラベルは解析し直すと入れ替わる(session: Session) -> None:
    _downloaded_document(session)
    save_document_labels(session, _DOC_ID, {"jppfs_cor_OperatingIncome": "古い文言"})

    save_document_labels(session, _DOC_ID, {"jppfs_cor_NetSales": "売上高"})

    rows = session.execute(select(EdinetDocumentLabel.concept)).scalars().all()
    assert rows == ["jppfs_cor_NetSales"]


def test_計算書は刷られた順に引ける(session: Session) -> None:
    """ビュー越しに ordinal で並べると有報の表の順になる."""
    _downloaded_document(session)
    save_labels(session, {"jppfs_cor_NetSales": "売上高"})
    save_document_labels(session, _DOC_ID, {"jppfs_cor_OperatingIncome": "セグメント利益"})
    session.add_all(
        [
            EdinetFact(
                **{
                    **_row(_fact("jppfs_cor_OperatingIncome", "CurrentYearDuration", "10")),
                    "ordinal": 5,
                    "depth": 2,
                }
            ),
            EdinetFact(
                **{
                    **_row(_fact("jppfs_cor_NetSales", "CurrentYearDuration", "100")),
                    "ordinal": 1,
                    "depth": 2,
                }
            ),
        ]
    )
    session.flush()

    rows = session.execute(
        text(
            "SELECT label, value FROM edinet_statement_lines "
            "WHERE doc_id = :doc_id ORDER BY ordinal"
        ),
        {"doc_id": _DOC_ID},
    ).all()

    assert [(r[0], int(r[1])) for r in rows] == [("売上高", 100), ("セグメント利益", 10)]


def _row(fact: Fact) -> dict[str, object]:
    return {
        "doc_id": _DOC_ID,
        "section": fact.section,
        "concept": fact.concept,
        "context_ref": fact.context_ref,
        "member": fact.member,
        "ordinal": fact.ordinal,
        "depth": fact.depth,
        "period_type": fact.period_type,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "value": fact.value,
        "unit": fact.unit,
        "decimals": fact.decimals,
    }


def test_大株主を取り込む(session: Session) -> None:
    _downloaded_document(session)

    saved = save_shareholders(
        session,
        _DOC_ID,
        "2168",
        date(2026, 5, 31),
        [
            Shareholder(
                rank=1,
                name="山田太郎",
                shares=1234000,
                ratio=Decimal("11.64"),
                kind="individual",
                is_owner=True,
            )
        ],
    )

    assert saved == 1
    holder = session.get(EdinetShareholder, (_DOC_ID, 1))
    assert holder is not None
    assert holder.ratio == Decimal("11.64")
    assert holder.is_owner is True


def test_大株主が取れない書類は0件で記録する(session: Session) -> None:
    """大株主が無いこと自体は失敗ではない。parsed_at を埋めて次回また読まないようにする."""
    _downloaded_document(session)

    assert save_shareholders(session, _DOC_ID, "2168", date(2026, 5, 31), []) == 0
    mark_parsed(session, _DOC_ID)
    session.flush()

    assert unparsed_documents(session) == []
