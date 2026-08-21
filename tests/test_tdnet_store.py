"""TDnet 開示メタデータの取り込みのテスト.

セッションは conftest のフィクスチャが用意する。テストの最後にロールバックされる。
"""

from dataclasses import replace
from datetime import date, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kabu_app.collectors.tdnet import TdnetDisclosureMeta
from kabu_app.models import TdnetDisclosure
from kabu_app.stores.tdnet import (
    count_expired,
    latest_disclosed_date,
    load_disclosures,
    mark_downloaded,
    pending_disclosures,
)

DAY = date(2026, 8, 14)
HORIZON = date(2026, 7, 21)

_BASE = TdnetDisclosureMeta(
    doc_id="140120260814521503",
    disclosed_date=DAY,
    disclosed_time=time(15, 0),
    sec_code="21680",
    code="2168",
    company_name="パソナグループ",
    title="2026年5月期 決算短信〔日本基準〕（連結）",
    markets="東",
    is_amendment=False,
    xbrl_file="081220260814521503.zip",
)


def _meta(**overrides: object) -> TdnetDisclosureMeta:
    return replace(_BASE, **overrides)  # type: ignore[arg-type]


def test_開示を取り込む(session: Session) -> None:
    assert load_disclosures(session, [_meta()]) == 1

    disclosure = session.get(TdnetDisclosure, "140120260814521503")
    assert disclosure is not None
    assert disclosure.code == "2168"
    assert disclosure.disclosed_time == time(15, 0)
    assert disclosure.downloaded_at is None


def test_同じ開示を2回入れても1行のまま(session: Session) -> None:
    """一覧は開示日単位で舐め直すので、同じ開示が何度も流れてくる."""
    load_disclosures(session, [_meta()])
    load_disclosures(session, [_meta(title="2026年5月期 決算短信〔日本基準〕（連結）（訂正後）")])

    assert session.execute(select(func.count()).select_from(TdnetDisclosure)).scalar_one() == 1


def test_取り込み直しても取得済みの記録は消えない(session: Session) -> None:
    load_disclosures(session, [_meta()])
    mark_downloaded(session, _BASE.doc_id)
    session.flush()

    load_disclosures(session, [_meta()])

    disclosure = session.get(TdnetDisclosure, _BASE.doc_id)
    assert disclosure is not None
    session.refresh(disclosure)
    assert disclosure.downloaded_at is not None


def test_期限内の未取得だけを返す(session: Session) -> None:
    """31 日より前は実体も消えている。何度叩いても 404 が返るだけになる."""
    load_disclosures(
        session,
        [
            _meta(doc_id="OLD", disclosed_date=date(2026, 6, 1)),
            _meta(doc_id="NEW", disclosed_date=DAY),
        ],
    )

    pending = pending_disclosures(session, HORIZON)

    assert [d.doc_id for d in pending] == ["NEW"]


def test_未取得の開示は開示日と時刻の順に返る(session: Session) -> None:
    load_disclosures(
        session,
        [
            _meta(doc_id="B", disclosed_time=time(17, 0)),
            _meta(doc_id="A", disclosed_time=time(9, 0)),
            _meta(doc_id="C", disclosed_date=date(2026, 8, 15), disclosed_time=time(8, 0)),
        ],
    )

    pending = pending_disclosures(session, HORIZON)

    assert [d.doc_id for d in pending] == ["A", "B", "C"]


def test_期限切れの未取得を数える(session: Session) -> None:
    """取り逃した数を出さないと、欠けたことに気づけないまま進む."""
    load_disclosures(
        session,
        [
            _meta(doc_id="OLD", disclosed_date=date(2026, 6, 1)),
            _meta(doc_id="NEW", disclosed_date=DAY),
        ],
    )

    assert count_expired(session, HORIZON) == 1


def test_最新の開示日を返す(session: Session) -> None:
    assert latest_disclosed_date(session) is None

    load_disclosures(
        session,
        [
            _meta(doc_id="OLD", disclosed_date=date(2026, 8, 1)),
            _meta(doc_id="NEW", disclosed_date=DAY),
        ],
    )

    assert latest_disclosed_date(session) == DAY


def test_取り込み上限を指定できる(session: Session) -> None:
    load_disclosures(session, [_meta(doc_id=f"DOC{index}") for index in range(3)])

    assert len(pending_disclosures(session, HORIZON, limit=2)) == 2
