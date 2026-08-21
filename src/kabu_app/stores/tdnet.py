"""TDnet の開示メタデータを tdnet_disclosures に書き込む.

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

from kabu_app.collectors.tdnet import TdnetDisclosureMeta
from kabu_app.models import TdnetDisclosure

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500

_UPDATABLE_COLUMNS = (
    "disclosed_date",
    "disclosed_time",
    "sec_code",
    "code",
    "company_name",
    "title",
    "markets",
    "is_amendment",
    "xbrl_file",
)
"""upsert で上書きする列。downloaded_at は含めない。取得済みの記録を消さないため."""


def latest_disclosed_date(session: Session) -> date | None:
    """取り込み済みで一番新しい開示日. 差分取得の起点に使う."""
    return session.execute(select(func.max(TdnetDisclosure.disclosed_date))).scalar_one_or_none()


def load_disclosures(session: Session, metas: Sequence[TdnetDisclosureMeta]) -> int:
    """開示メタデータを取り込む. コミットは呼び出し側の責任.

    銘柄マスタと突き合わせて絞り込みはしない。上場廃止した会社の開示も残す。理由は
    edinet_documents と同じで、過去を評価するときの生存者バイアスを避けるため。
    """
    if not metas:
        return 0

    unique = list({meta.doc_id: meta for meta in metas}.values())

    for chunk in _chunked([asdict(meta) for meta in unique]):
        statement = insert(TdnetDisclosure).values(list(chunk))
        statement = statement.on_conflict_do_update(
            index_elements=[TdnetDisclosure.doc_id],
            set_={
                **{column: statement.excluded[column] for column in _UPDATABLE_COLUMNS},
                # onupdate は ORM の更新でしか効かないので明示する
                "updated_at": func.now(),
            },
        )
        session.execute(statement)

    session.flush()
    return len(unique)


def pending_disclosures(
    session: Session, since: date, limit: int | None = None
) -> Sequence[TdnetDisclosure]:
    """実体をまだ取れていない開示を古い順に返す.

    since より前は取りに行かない。TDnet は一覧も実体も 31 日ほどで消えるため、
    それより古いものを何度叩いても 404 が返るだけになる。
    """
    statement = (
        select(TdnetDisclosure)
        .where(
            TdnetDisclosure.downloaded_at.is_(None),
            TdnetDisclosure.disclosed_date >= since,
        )
        .order_by(TdnetDisclosure.disclosed_date, TdnetDisclosure.disclosed_time)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return session.execute(statement).scalars().all()


def mark_downloaded(session: Session, doc_id: str) -> None:
    """実体を保存した記録を付ける. コミットは呼び出し側の責任."""
    session.execute(
        update(TdnetDisclosure)
        .where(TdnetDisclosure.doc_id == doc_id)
        .values(downloaded_at=func.now(), updated_at=func.now())
    )


def count_expired(session: Session, since: date) -> int:
    """取り逃したまま期限が切れた開示の数. 二度と取れない."""
    return session.execute(
        select(func.count())
        .select_from(TdnetDisclosure)
        .where(
            TdnetDisclosure.downloaded_at.is_(None),
            TdnetDisclosure.disclosed_date < since,
        )
    ).scalar_one()


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]
