"""名寄せした財務項目を DB に書き込む.

同じ書類を 2 回名寄せしても壊れない。書類単位で消してから入れ直す。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.models import (
    EdinetDocument,
    EdinetDocumentLabel,
    EdinetFact,
    EdinetFinancial,
    EdinetLabel,
    Stock,
)
from kabu_app.normalizers.financials import FinancialValue, SourceFact

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1000

_SECTIONS = ("BR", "PL", "BS")
"""名寄せで見るセクション. 引く量を絞るために先に落とす"""

NON_CONSOLIDATED_MEMBER = "NonConsolidatedMember"
"""単体決算の会社が数値に付ける member.

連結財務諸表を作らない会社は、経営指標の推移も損益計算書もすべてこの member 付きで書く。
実測では単体決算 765 書類のうち、BR セクションに ``member IS NULL`` の数値を持つものが
1 件も無かった。連結企業と同じ条件で引くと、この 765 書類から 1 項目も取れない。
"""


def documents_to_normalize(
    session: Session, limit: int | None = None, renormalize: bool = False
) -> Sequence[EdinetDocument]:
    """解析済みで、まだ名寄せしていない書類を古い順に返す.

    ``renormalize`` を立てると名寄せ済みも返す。項目の定義を直して全件を作り直すとき用。

    会計年度末が入っていない書類は外す。どの期の書類か決まらないと、期ごとの最新を
    選べないため。解析すれば埋まるので、次の ``kabu parse edinet`` が拾う。

    stocks に無い銘柄も外す。投資の対象はプライム・スタンダード・グロースの内国株に限る
    ので、それ以外の財務項目を持っても引く場面が無い。ここに来るのはほとんどが上場廃止した
    会社で、株価が取れないためバックテストにも使えない。実測では 271 銘柄のうち 192 銘柄に
    ticks が 1 行も無かった。

    絞るのは名寄せの入口であって、取得ではない。edinet_documents と edinet_facts は全部
    残す。方針を変えたときに --renormalize で作り直せる。
    """
    normalized = select(EdinetFinancial.doc_id).distinct().scalar_subquery()
    statement: Select[tuple[EdinetDocument]] = (
        select(EdinetDocument)
        .join(Stock, Stock.code == EdinetDocument.code)
        .where(
            EdinetDocument.parsed_at.is_not(None),
            EdinetDocument.fiscal_year_end.is_not(None),
        )
        .order_by(EdinetDocument.submit_date, EdinetDocument.doc_id)
    )
    if not renormalize:
        statement = statement.where(EdinetDocument.doc_id.not_in(normalized))
    if limit is not None:
        statement = statement.limit(limit)
    return session.execute(statement).scalars().all()


def load_source_facts(
    session: Session, doc_id: str, is_consolidated: bool | None
) -> list[SourceFact]:
    """名寄せの入力を 1 書類分そろえる.

    連結企業は ``member IS NULL`` の数値だけを返す。セグメント別の値が混ざると、同じ期に
    複数の候補が並んで取り違える。連結企業の単体の数値も ``NonConsolidatedMember`` が
    付くので、ここで一緒に落ちる。

    単体決算の会社は ``NonConsolidatedMember`` も通す。この会社にとってはそれが唯一の
    数値になる。``NonConsolidatedMember_ShareholdersEquityMember`` のように後ろが続くもの
    は株主資本等変動計算書の内訳なので、完全一致だけを見て通さない。

    ラベルは書類に同梱されたものを優先する。会社は標準の勘定に独自の言い換えを付ける
    ため、その書類での文言でないと拾い損ねる。無ければタクソノミの標準ラベルを使う。
    """
    document_label = (
        select(EdinetDocumentLabel.label)
        .where(
            EdinetDocumentLabel.doc_id == EdinetFact.doc_id,
            EdinetDocumentLabel.concept == EdinetFact.concept,
        )
        .scalar_subquery()
    )
    standard_label = (
        select(EdinetLabel.label).where(EdinetLabel.concept == EdinetFact.concept).scalar_subquery()
    )

    member_filter = (
        EdinetFact.member.is_(None)
        if is_consolidated
        else or_(EdinetFact.member.is_(None), EdinetFact.member == NON_CONSOLIDATED_MEMBER)
    )

    rows = session.execute(
        select(
            EdinetFact.section,
            EdinetFact.concept,
            EdinetFact.period_type,
            EdinetFact.period_start,
            EdinetFact.period_end,
            EdinetFact.value,
            EdinetFact.unit,
            func.coalesce(document_label, standard_label).label("label"),
        ).where(
            EdinetFact.doc_id == doc_id,
            member_filter,
            EdinetFact.section.in_(_SECTIONS),
        )
    ).all()

    return [
        SourceFact(
            section=row.section,
            concept=row.concept,
            period_type=row.period_type,
            period_start=row.period_start,
            period_end=row.period_end,
            value=row.value,
            unit=row.unit,
            label=row.label,
        )
        for row in rows
    ]


def save_financials(session: Session, doc_id: str, values: Sequence[FinancialValue]) -> int:
    """名寄せの結果を入れ替える. コミットは呼び出し側の責任."""
    session.execute(delete(EdinetFinancial).where(EdinetFinancial.doc_id == doc_id))
    if not values:
        return 0

    rows = [
        {
            "doc_id": doc_id,
            "item": value.item,
            "period_end": value.period_end,
            "period_start": value.period_start,
            "value": value.value,
            "unit": value.unit,
            "source_section": value.source_section,
            "source_concept": value.source_concept,
        }
        for value in values
    ]
    for start in range(0, len(rows), _CHUNK_SIZE):
        session.execute(insert(EdinetFinancial), rows[start : start + _CHUNK_SIZE])

    session.flush()
    return len(rows)
