"""有報の解析結果を DB に書き込む.

同じ書類を 2 回解析しても壊れない。書類単位で消してから入れ直す。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.collectors.edinet import TARGET_DOC_TYPES
from kabu_app.models import (
    EdinetDocument,
    EdinetDocumentLabel,
    EdinetFact,
    EdinetLabel,
    EdinetShareholder,
)
from kabu_app.parsers.edinet_xbrl import Fact
from kabu_app.parsers.shareholders import Shareholder

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1000


def unparsed_documents(
    session: Session, limit: int | None = None, include_parsed: bool = False
) -> Sequence[EdinetDocument]:
    """ZIP は取れているが、まだ解析していない書類を古い順に返す.

    前回失敗した書類も parsed_at が NULL のまま残るので、次の実行がここで拾い直す。
    直らない書類を毎回引き当てることになるが、件数は parse_error を見れば分かる。

    ``include_parsed`` を立てると解析済みも返す。パーサを直して全件を取り直すとき用。

    訂正有価証券報告書 (130) も解析する。様式は有報と同じで、同じ手順でそのまま読める。
    585 組を元と比べると 74 組 (12.6%) で純資産・EPS・ROE といった数値が動いていた。

    訂正報告書は差分ではなく全文になる (585 組すべてでファクト数の比が 0.99〜1.02)。元とは
    マージせず、書類ごとに丸ごと入れる。期ごとの最新は edinet_latest_facts が選ぶ。マージは
    むしろ誤りで、赤字転落で消えた PER を元から拾い直すことになる。
    """
    statement = (
        select(EdinetDocument)
        .where(
            EdinetDocument.downloaded_at.is_not(None),
            EdinetDocument.doc_type_code.in_(TARGET_DOC_TYPES),
            EdinetDocument.is_withdrawn.is_(False),
        )
        .order_by(EdinetDocument.submit_date, EdinetDocument.doc_id)
    )
    if not include_parsed:
        statement = statement.where(EdinetDocument.parsed_at.is_(None))
    if limit is not None:
        statement = statement.limit(limit)
    return session.execute(statement).scalars().all()


def documents_by_id(session: Session, doc_ids: Sequence[str]) -> Sequence[EdinetDocument]:
    """書類管理番号を指定して引く. 解析済みかどうかは見ない.

    パーサを直したあと、問題のあった書類だけを確かめるために使う。
    """
    if not doc_ids:
        return []
    return (
        session.execute(
            select(EdinetDocument)
            .where(EdinetDocument.doc_id.in_(doc_ids))
            .order_by(EdinetDocument.submit_date, EdinetDocument.doc_id)
        )
        .scalars()
        .all()
    )


def save_facts(session: Session, doc_id: str, facts: Sequence[Fact]) -> int:
    """ファクトを入れ替える. コミットは呼び出し側の責任."""
    session.execute(delete(EdinetFact).where(EdinetFact.doc_id == doc_id))
    if not facts:
        return 0

    rows = [
        {
            "doc_id": doc_id,
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
        for fact in facts
    ]
    for chunk in _chunked(rows):
        session.execute(insert(EdinetFact), list(chunk))

    session.flush()
    return len(rows)


def save_labels(session: Session, labels: dict[str, str]) -> int:
    """タクソノミの標準ラベルを取り込む. 同じ要素名が既にあれば上書きする.

    書類に同梱されたラベルはここに入れない。会社ごとに文言が違うため、全社で 1 行に
    まとめると他社の言い換えが混ざる。``save_document_labels`` に分けてある。
    """
    if not labels:
        return 0

    rows = [{"concept": concept, "label": label[:500]} for concept, label in labels.items()]
    for chunk in _chunked(rows):
        statement = insert(EdinetLabel).values(list(chunk))
        statement = statement.on_conflict_do_update(
            index_elements=[EdinetLabel.concept],
            set_={"label": statement.excluded.label, "updated_at": func.now()},
        )
        session.execute(statement)

    session.flush()
    return len(rows)


def save_document_labels(session: Session, doc_id: str, labels: dict[str, str]) -> int:
    """書類に同梱されていたラベルを入れ替える. コミットは呼び出し側の責任."""
    session.execute(delete(EdinetDocumentLabel).where(EdinetDocumentLabel.doc_id == doc_id))
    if not labels:
        return 0

    rows = [
        {"doc_id": doc_id, "concept": concept, "label": label[:500]}
        for concept, label in labels.items()
    ]
    for chunk in _chunked(rows):
        session.execute(insert(EdinetDocumentLabel), list(chunk))

    session.flush()
    return len(rows)


def save_shareholders(
    session: Session,
    doc_id: str,
    code: str,
    period_end: date | None,
    shareholders: Sequence[Shareholder],
) -> int:
    """大株主を入れ替える. コミットは呼び出し側の責任."""
    session.execute(delete(EdinetShareholder).where(EdinetShareholder.doc_id == doc_id))
    if not shareholders:
        return 0

    session.execute(
        insert(EdinetShareholder),
        [
            {
                "doc_id": doc_id,
                "rank": holder.rank,
                "code": code,
                "period_end": period_end,
                "name": holder.name[:200],
                "shares": holder.shares,
                "ratio": holder.ratio,
                "kind": holder.kind,
                "is_owner": holder.is_owner,
            }
            for holder in shareholders
        ],
    )
    session.flush()
    return len(shareholders)


def mark_parsed(
    session: Session,
    doc_id: str,
    fiscal_year_end: date | None = None,
    error: str | None = None,
) -> None:
    """解析の結果を書類に記録する. コミットは呼び出し側の責任.

    失敗したときは parsed_at を空のままにする。次の実行が拾い直せるようにするため。
    理由だけ parse_error に残す。

    ``fiscal_year_end`` は XBRL の DEI から読んだ会計年度末。訂正有報には API が期を返さ
    ないので、ここで埋めないと期ごとの最新版を選べない。
    """
    values: dict[str, Any] = {
        "parsed_at": None if error is not None else func.now(),
        # 成功したら前回の失敗の記録を消す
        "parse_error": error[:2000] if error is not None else None,
        "updated_at": func.now(),
    }
    if fiscal_year_end is not None:
        values["fiscal_year_end"] = fiscal_year_end

    session.execute(update(EdinetDocument).where(EdinetDocument.doc_id == doc_id).values(**values))


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]
