"""EDINET 書類メタデータの取り込みのテスト.

セッションは conftest のフィクスチャが用意する。テストの最後にロールバックされる。
"""

from dataclasses import replace
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kabu_app.collectors.edinet import EdinetDocumentMeta
from kabu_app.models import EdinetDocument
from kabu_app.stores.edinet import (
    latest_submit_date,
    load_documents,
    mark_downloaded,
    pending_documents,
)

_BASE = EdinetDocumentMeta(
    doc_id="S100YW7F",
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


def _meta(**overrides: object) -> EdinetDocumentMeta:
    return replace(_BASE, **overrides)  # type: ignore[arg-type]


def test_書類を取り込む(session: Session) -> None:
    assert load_documents(session, [_meta()]) == 1

    document = session.get(EdinetDocument, "S100YW7F")
    assert document is not None
    assert document.code == "2168"
    assert document.period_end == date(2026, 5, 31)
    assert document.downloaded_at is None


def test_銘柄マスタに無いコードも取り込む(session: Session) -> None:
    """上場廃止した会社の有報を捨てると、過去の評価に生存者バイアスが入る.

    stocks は JPX の最新一覧から作るので、買収や MBO で消えた会社は載らない。
    """
    assert load_documents(session, [_meta(doc_id="S100XXXX", code="7732")]) == 1

    document = session.get(EdinetDocument, "S100XXXX")
    assert document is not None
    assert document.code == "7732"


def test_同じ書類を2回入れても1行のまま(session: Session) -> None:
    """一覧は提出日単位で舐め直すので、同じ書類が何度も流れてくる."""
    load_documents(session, [_meta()])
    load_documents(session, [_meta(doc_description="有価証券報告書－第49期（訂正後）")])

    assert session.execute(select(func.count()).select_from(EdinetDocument)).scalar_one() == 1
    document = session.get(EdinetDocument, "S100YW7F")
    assert document is not None
    session.refresh(document)
    assert document.doc_description == "有価証券報告書－第49期（訂正後）"


def test_取り込み直しても取得済みの記録は消えない(session: Session) -> None:
    """downloaded_at を上書きすると、取得済みの ZIP を毎回落とし直すことになる."""
    load_documents(session, [_meta()])
    mark_downloaded(session, "S100YW7F")
    session.flush()

    load_documents(session, [_meta()])

    document = session.get(EdinetDocument, "S100YW7F")
    assert document is not None
    session.refresh(document)
    assert document.downloaded_at is not None


def test_未取得の書類だけを返す(session: Session) -> None:
    load_documents(session, [_meta(), _meta(doc_id="S100YWGX", code="9278")])
    mark_downloaded(session, "S100YW7F")
    session.flush()

    pending = pending_documents(session)

    assert [document.doc_id for document in pending] == ["S100YWGX"]


def test_取り下げとXBRLの無い書類は取りに行かない(session: Session) -> None:
    load_documents(
        session,
        [
            _meta(doc_id="S100AAAA", is_withdrawn=True),
            _meta(doc_id="S100BBBB", has_xbrl=False),
            _meta(doc_id="S100CCCC"),
        ],
    )

    pending = pending_documents(session)

    assert [document.doc_id for document in pending] == ["S100CCCC"]


def test_未取得の書類は提出日が古い順に返る(session: Session) -> None:
    """バックフィルの途中で止めても、続きから拾える順序にする."""
    load_documents(
        session,
        [
            _meta(doc_id="S100NEW0", submit_date=date(2026, 8, 14)),
            _meta(doc_id="S100OLD0", submit_date=date(2025, 1, 6)),
        ],
    )

    pending = pending_documents(session)

    assert [document.doc_id for document in pending] == ["S100OLD0", "S100NEW0"]


def test_最新の提出日を返す(session: Session) -> None:
    """差分取得の起点に使う."""
    assert latest_submit_date(session) is None

    load_documents(
        session,
        [
            _meta(doc_id="S100OLD0", submit_date=date(2025, 1, 6)),
            _meta(doc_id="S100NEW0", submit_date=date(2026, 8, 14)),
        ],
    )

    assert latest_submit_date(session) == date(2026, 8, 14)


def test_取り込み上限を指定できる(session: Session) -> None:
    """1 回の実行で落とす件数を絞れないと、バックフィルの初回が長時間走る."""
    load_documents(session, [_meta(doc_id=f"S100000{index}") for index in range(3)])

    assert len(pending_documents(session, limit=2)) == 2


def test_一覧に同じ書類が2回出ても落ちない(session: Session) -> None:
    """EDINET は同じレコードを 2 行返すことがある。2025-06-10 の S100VWD9 がこれ."""
    assert load_documents(session, [_meta(), _meta()]) == 1

    assert session.execute(select(func.count()).select_from(EdinetDocument)).scalar_one() == 1
