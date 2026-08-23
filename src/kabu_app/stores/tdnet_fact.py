"""決算短信の解析結果を DB に書き込む.

同じ書類を 2 回解析しても壊れない。書類単位で消してから入れ直す。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.models import TdnetDisclosure, TdnetStatementFact, TdnetSummaryFact
from kabu_app.parsers.tdnet_xbrl import DisclosureInfo, StatementFact, SummaryFact

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1000


def unparsed_disclosures(
    session: Session, limit: int | None = None, include_parsed: bool = False
) -> Sequence[TdnetDisclosure]:
    """実体は取れているが、まだ解析していない開示を古い順に返す.

    前回失敗した開示も parsed_at が NULL のまま残るので、次の実行がここで拾い直す。

    ``xbrl_file`` では絞らない。findocgen から移した 25,702 件はこの列が NULL のままで、
    XBRL が無いのではなく**有無が分からない**だけになる。実体は全件そろっている。ここで
    絞ると過去分がまるごと対象から外れる。XBRL の付かない短信は ZIP が置き場に無いので、
    解析のときに気づいて ``mark_no_xbrl`` で片付ける。

    訂正短信も解析する。様式は同じで、そのまま読める。元とはマージせず書類ごとに丸ごと
    入れる。有報と同じ考え方になる。
    """
    statement: Select[tuple[TdnetDisclosure]] = (
        select(TdnetDisclosure)
        .where(TdnetDisclosure.downloaded_at.is_not(None))
        .order_by(TdnetDisclosure.disclosed_date, TdnetDisclosure.doc_id)
    )
    if not include_parsed:
        statement = statement.where(TdnetDisclosure.parsed_at.is_(None))
    if limit is not None:
        statement = statement.limit(limit)
    return session.execute(statement).scalars().all()


def disclosures_by_id(session: Session, doc_ids: Sequence[str]) -> Sequence[TdnetDisclosure]:
    """書類 ID を指定して引く. 解析済みかどうかは見ない."""
    if not doc_ids:
        return []
    return (
        session.execute(
            select(TdnetDisclosure)
            .where(TdnetDisclosure.doc_id.in_(doc_ids))
            .order_by(TdnetDisclosure.disclosed_date, TdnetDisclosure.doc_id)
        )
        .scalars()
        .all()
    )


def save_summary_facts(session: Session, doc_id: str, facts: Sequence[SummaryFact]) -> int:
    """表紙の数値を入れ替える. コミットは呼び出し側の責任."""
    session.execute(delete(TdnetSummaryFact).where(TdnetSummaryFact.doc_id == doc_id))
    if not facts:
        return 0

    rows = [
        {
            "doc_id": doc_id,
            "concept": fact.concept,
            "context_ref": fact.context_ref,
            "scope": fact.scope,
            "period_kind": fact.period_kind,
            "quarter_member": fact.quarter_member,
            "is_consolidated": fact.is_consolidated,
            "fact_type": fact.fact_type,
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
        session.execute(insert(TdnetSummaryFact), list(chunk))

    session.flush()
    return len(rows)


def save_statement_facts(session: Session, doc_id: str, facts: Sequence[StatementFact]) -> int:
    """財務諸表の数値を入れ替える. コミットは呼び出し側の責任."""
    session.execute(delete(TdnetStatementFact).where(TdnetStatementFact.doc_id == doc_id))
    if not facts:
        return 0

    rows = [
        {
            "doc_id": doc_id,
            "section": fact.section,
            "concept": fact.concept,
            "context_ref": fact.context_ref,
            "term": fact.term,
            "is_consolidated": fact.is_consolidated,
            "member": fact.member,
            "ordinal": fact.ordinal,
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
        session.execute(insert(TdnetStatementFact), list(chunk))

    session.flush()
    return len(rows)


def mark_parsed(
    session: Session,
    doc_id: str,
    info: DisclosureInfo | None = None,
    error: str | None = None,
) -> None:
    """解析の結果を開示に記録する. コミットは呼び出し側の責任.

    失敗したときは parsed_at を空のままにする。次の実行が拾い直せるようにするため。
    理由だけ parse_error に残す。

    ``info`` は表紙から読んだ書類の素性。有報の DEI にあたるものが短信には無いので、
    表題から会計基準と連結の有無を読む。
    """
    values: dict[str, Any] = {
        "parsed_at": None if error is not None else func.now(),
        # 成功したら前回の失敗の記録を消す
        "parse_error": error[:2000] if error is not None else None,
        "updated_at": func.now(),
    }
    if info is not None:
        values["accounting_standard"] = info.accounting_standard
        values["is_consolidated"] = info.is_consolidated
        values["fiscal_year_end"] = info.fiscal_year_end
        values["quarter"] = info.quarter

    session.execute(
        update(TdnetDisclosure).where(TdnetDisclosure.doc_id == doc_id).values(**values)
    )


def mark_no_xbrl(session: Session, doc_id: str) -> None:
    """XBRL の無い短信として片付ける. コミットは呼び出し側の責任.

    決算短信の 1 割ほどに XBRL が付かない。中間決算短信で目立つ。数値はどうやっても
    取れないので、失敗ではなく解析済みとして扱う。次の実行が拾い直さないよう parsed_at を
    立て、なぜ 0 件なのかを parse_error に残す。

    「表題に決算短信を含むが短信そのものではない開示」もここに来る。発表日変更のお知らせ
    などで、こちらは PDF すら XBRL を持たない。
    """
    session.execute(
        update(TdnetDisclosure)
        .where(TdnetDisclosure.doc_id == doc_id)
        .values(
            parsed_at=func.now(),
            parse_error="XBRL の ZIP が置き場に無い。PDF だけの短信",
            updated_at=func.now(),
        )
    )


def _chunked(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK_SIZE):
        yield rows[start : start + _CHUNK_SIZE]
