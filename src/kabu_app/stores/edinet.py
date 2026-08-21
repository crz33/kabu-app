"""EDINET の書類メタデータを edinet_documents に書き込む.

同じ日を 2 回処理しても壊れない。doc_id で upsert する。
"""

import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.collectors.edinet import EdinetDocumentMeta
from kabu_app.models import EdinetDocument

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500

_UPDATABLE_COLUMNS = (
    "edinet_code",
    "sec_code",
    "code",
    "doc_type_code",
    "parent_doc_id",
    "submit_date",
    "submitted_at",
    "period_end",
    "filer_name",
    "doc_description",
    "has_xbrl",
    "is_withdrawn",
)
"""upsert で上書きする列。downloaded_at は含めない。取得済みの記録を消さないため."""


def latest_submit_date(session: Session) -> date | None:
    """取り込み済みで一番新しい提出日. 差分取得の起点に使う."""
    return session.execute(select(func.max(EdinetDocument.submit_date))).scalar_one_or_none()


def load_documents(session: Session, metas: Sequence[EdinetDocumentMeta]) -> int:
    """書類メタデータを取り込む. コミットは呼び出し側の責任.

    銘柄マスタと突き合わせて絞り込みはしない。上場廃止した会社の有報も残す。今の上場銘柄
    だけを集めると、過去を評価するときに生存者バイアスが入るため。上場中に絞りたい分析は
    stocks と結合する。
    """
    if not metas:
        return 0

    for chunk in _chunked([asdict(meta) for meta in metas]):
        statement = insert(EdinetDocument).values(list(chunk))
        statement = statement.on_conflict_do_update(
            index_elements=[EdinetDocument.doc_id],
            set_={
                **{column: statement.excluded[column] for column in _UPDATABLE_COLUMNS},
                # onupdate は ORM の更新でしか効かないので明示する
                "updated_at": func.now(),
            },
        )
        session.execute(statement)

    session.flush()
    return len(metas)


def pending_documents(session: Session, limit: int | None = None) -> Sequence[EdinetDocument]:
    """ZIP をまだ取れていない書類を古い順に返す.

    取り下げられた書類と XBRL の無い書類は取りに行かない。前回の実行で失敗した書類も
    downloaded_at が NULL のまま残るので、次の実行がここで拾い直す。
    """
    statement = (
        select(EdinetDocument)
        .where(
            EdinetDocument.downloaded_at.is_(None),
            EdinetDocument.has_xbrl.is_(True),
            EdinetDocument.is_withdrawn.is_(False),
        )
        .order_by(EdinetDocument.submit_date, EdinetDocument.doc_id)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return session.execute(statement).scalars().all()


def mark_downloaded(session: Session, doc_id: str) -> None:
    """ZIP を保存した記録を付ける. コミットは呼び出し側の責任."""
    session.execute(
        update(EdinetDocument)
        .where(EdinetDocument.doc_id == doc_id)
        .values(downloaded_at=func.now(), updated_at=func.now())
    )


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]
