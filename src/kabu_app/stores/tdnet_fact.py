"""決算短信の解析結果を DB に書き込む.

同じ書類を 2 回解析しても壊れない。書類単位で消してから入れ直す。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kabu_app.models import (
    Stock,
    TdnetDisclosure,
    TdnetFinancial,
    TdnetStatementFact,
    TdnetSummaryFact,
)
from kabu_app.normalizers.tdnet_financials import FinancialValue as TdnetFinancialValue
from kabu_app.normalizers.tdnet_financials import SourceFact as TdnetSourceFact
from kabu_app.normalizers.tdnet_financials import (
    SummarySourceFact as TdnetSummarySourceFact,
)
from kabu_app.parsers.tdnet_xbrl import DisclosureInfo, StatementFact, SummaryFact
from kabu_app.stores.edinet_financial import NON_CONSOLIDATED_MEMBER

logger = logging.getLogger(__name__)

_SUMMARY_RESULT = "Result"
"""表紙の fact_type のうち実績を表す値. Forecast は会社予想なので名寄せに入れない."""

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


def disclosures_to_normalize(
    session: Session, limit: int | None = None, renormalize: bool = False
) -> Sequence[TdnetDisclosure]:
    """解析済みで、まだ名寄せしていない開示を古い順に返す.

    ``renormalize`` を立てると名寄せ済みも返す。項目の定義を直して全件を作り直すとき用。

    XBRL の無い短信は外す。``mark_no_xbrl`` が理由を残しているので、それで見分ける。

    stocks に無い銘柄も外す。TDnet の取得は表題に「決算短信」が入るものを全部拾うので、
    REIT や地方市場の単独上場銘柄が混ざる。stocks はプライム・スタンダード・グロースの
    内国株だけを持つため、結合すればその 3 区分に絞れる。

    絞るのは名寄せの入口であって、取得ではない。TDnet の開示は 31 日で消えるので、取得時に
    落とすと二度と取れない。新規上場した銘柄が JPX 銘柄一覧に載るのは月次更新のあとになり、
    それまでの短信を取り逃す。生データは全部残して、出口で絞る。

    上場廃止した銘柄は stocks に is_listed = false で残るので、ここでは落ちない。過去の
    決算を評価するときに生存者バイアスが入らないようにする。
    """
    normalized = select(TdnetFinancial.doc_id).distinct().scalar_subquery()
    statement: Select[tuple[TdnetDisclosure]] = (
        select(TdnetDisclosure)
        .join(Stock, Stock.code == TdnetDisclosure.code)
        .where(
            TdnetDisclosure.parsed_at.is_not(None),
            TdnetDisclosure.parse_error.is_(None),
        )
        .order_by(TdnetDisclosure.disclosed_date, TdnetDisclosure.doc_id)
    )
    if not renormalize:
        statement = statement.where(TdnetDisclosure.doc_id.not_in(normalized))
    if limit is not None:
        statement = statement.limit(limit)
    return session.execute(statement).scalars().all()


def load_statement_source_facts(
    session: Session, doc_id: str, is_consolidated: bool | None
) -> list[TdnetSourceFact]:
    """名寄せの入力を 1 書類分そろえる.

    絞りには 2 つの軸が要る。添付はファイルが連結と単体で分かれており、その中の context に
    も区分が入る。連結企業は連結のファイルを見て ``member IS NULL`` を採る。連結企業でも
    単体の計算書を出すことがあり、そちらは ``NonConsolidatedMember`` が付くのでここで落ちる。

    単体決算の会社は ``NonConsolidatedMember`` も通す。この会社にとってはそれが唯一の
    数値になる。実測 763 書類では ``member IS NULL`` の行が書類あたり 1 つしかなく、中身は
    提出回数の DEI だった。有報とまったく同じ構造で、連結企業と同じ条件で引くとこの 763
    書類から 1 項目も取れない。

    連結の指定が無い書類は、添付に入っているほうを採る。表紙 (``XBRLData/Summary/``) を
    持たない ZIP があり、書類の素性を読む先が無いまま解析済みになる。実測 22 件がこれで、
    14 件は単体、8 件は連結だった。連結と単体が混ざる書類は無い。

    ここを「指定が無ければ連結」と決め打つと、単体決算の 14 件から 1 項目も取れない。
    添付は単体の行しか持たず、連結で絞ると 0 件になるため。

    セグメント別の値は落とす。損益計算書と貸借対照表だけを見る。包括利益・株主資本等変動・
    セグメントに 6 項目は入らない。

    ``PC`` は ``PL`` として扱う。``PC`` は損益及び包括利益計算書で、包括利益を 1 つの計算書に
    まとめる方式で出る。売上高から営業利益までは通常の損益計算書と同じ要素が同じ順で並び、
    後ろに包括利益が続くだけになる。実測 230 書類が該当し、そのすべてで ``CI`` が入って
    いなかった。落とすとその会社がまるごと空になる。

    ``PL`` と両方を持つ書類も 9 件ある。同じ期の同じ項目は 1 つに絞られるのでぶつからない。
    ``tdnet_statement_facts`` では別の section のまま残し、ここでだけ寄せる。
    """
    if is_consolidated is None:
        is_consolidated = _consolidated_flag_in_attachment(session, doc_id)

    member_filter = (
        TdnetStatementFact.member.is_(None)
        if is_consolidated is not False
        else or_(
            TdnetStatementFact.member.is_(None),
            TdnetStatementFact.member == NON_CONSOLIDATED_MEMBER,
        )
    )

    rows = session.execute(
        select(
            TdnetStatementFact.section,
            TdnetStatementFact.concept,
            TdnetStatementFact.context_ref,
            TdnetStatementFact.period_type,
            TdnetStatementFact.period_start,
            TdnetStatementFact.period_end,
            TdnetStatementFact.value,
            TdnetStatementFact.unit,
        ).where(
            TdnetStatementFact.doc_id == doc_id,
            member_filter,
            TdnetStatementFact.section.in_(("PL", "PC", "BS")),
            TdnetStatementFact.is_consolidated.is_(is_consolidated is not False),
        )
    ).all()

    return [
        TdnetSourceFact(
            section="PL" if row.section == "PC" else row.section,
            concept=row.concept,
            context_ref=row.context_ref,
            period_type=row.period_type,
            period_start=row.period_start,
            period_end=row.period_end,
            value=row.value,
            unit=row.unit,
        )
        for row in rows
    ]


def save_tdnet_financials(
    session: Session, doc_id: str, values: Sequence[TdnetFinancialValue]
) -> int:
    """名寄せの結果を入れ替える. コミットは呼び出し側の責任."""
    session.execute(delete(TdnetFinancial).where(TdnetFinancial.doc_id == doc_id))
    if not values:
        return 0

    rows = [
        {
            "doc_id": doc_id,
            "item": value.item,
            "period_kind": value.period_kind,
            "period_end": value.period_end,
            "period_start": value.period_start,
            "value": value.value,
            "unit": value.unit,
            "source_section": value.source_section,
            "source_concept": value.source_concept,
        }
        for value in values
    ]
    for chunk in _chunked(rows):
        session.execute(insert(TdnetFinancial), list(chunk))

    session.flush()
    return len(rows)


def load_summary_source_facts(
    session: Session, doc_id: str, is_consolidated: bool | None
) -> list[TdnetSummarySourceFact]:
    """表紙の名寄せの入力を 1 書類分そろえる.

    添付が空の書類でだけ使う。米国基準の会社は決算短信の添付 XBRL を出さず、数値データの
    訂正短信も表紙だけを出し直す。どちらも表紙には 6 項目が揃っている。

    会社予想を外す。表紙には当期の予想が実績と同じ要素名で載り、``fact_type`` だけが
    ``Forecast`` になる。混ぜると予想を実績として取り込む。

    前期は残す。``scope`` が ``Prior`` の行は前年同期の実績で、``period_end`` が違うので
    当期とぶつからない。

    連結と単体の選び方は添付と同じ。連結企業は連結の行を採り、単体決算の会社は単体を採る。
    連結の指定が無い書類は連結として扱う。
    """
    rows = session.execute(
        select(
            TdnetSummaryFact.concept,
            TdnetSummaryFact.period_kind,
            TdnetSummaryFact.period_type,
            TdnetSummaryFact.period_start,
            TdnetSummaryFact.period_end,
            TdnetSummaryFact.value,
            TdnetSummaryFact.unit,
        ).where(
            TdnetSummaryFact.doc_id == doc_id,
            TdnetSummaryFact.fact_type == _SUMMARY_RESULT,
            TdnetSummaryFact.is_consolidated.is_(is_consolidated is not False),
        )
    ).all()

    return [
        TdnetSummarySourceFact(
            concept=row.concept,
            period_kind=row.period_kind,
            period_type=row.period_type,
            period_start=row.period_start,
            period_end=row.period_end,
            value=row.value,
            unit=row.unit,
        )
        for row in rows
    ]


def _consolidated_flag_in_attachment(session: Session, doc_id: str) -> bool:
    """添付に入っている連結・単体の別を返す. 表紙を持たない書類の素性を決めるのに使う.

    連結の行が 1 つでもあれば連結として扱う。実測では連結と単体が混ざる書類は無く、
    どちらか一方しか入っていなかった。行が 1 つも無ければ連結を返す。どちらで引いても
    0 件になるので、既定を変えても結果は変わらない。
    """
    return bool(
        session.execute(
            select(TdnetStatementFact.doc_id)
            .where(
                TdnetStatementFact.doc_id == doc_id,
                TdnetStatementFact.is_consolidated.is_(True),
            )
            .limit(1)
        ).first()
    )
