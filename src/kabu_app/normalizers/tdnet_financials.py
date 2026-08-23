"""決算短信の添付を、有報と同じ財務項目に名寄せする.

添付は ``jppfs_cor`` / ``jpigp_cor`` と有報とまったく同じ体系なので、項目の定義は
:data:`kabu_app.normalizers.financials.ITEM_SPECS` をそのまま使う。実測では PL / BS の
エントリ 18 個のうち 15 個が短信でも当たり、売上の取りこぼしは無かった。定義を 2 つ持つと
片方だけ直す事故が起きるので、共有する。

有報と違うのは期の数え方になる。有報は ``period_end`` だけで期が決まった。短信は同じ期末に
**年初来累計と単独四半期**が並ぶ。「Q2 累計 100 億」と「Q2 単独 50 億」を取り違えると
致命的なので、``period_kind`` で区別する。

``period_start`` から期間の長さを計算しても大半は判別できる。しかし決算期を変えた会社で
狂う。実際に「第５四半期決算短信」を出す会社がある。context に書いてあるものを読むほうが
確かなので、そちらを採る。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from kabu_app.normalizers.financials import ITEM_SPECS, ItemSpec

YTD = "ytd"
QUARTER = "quarter"
YEAR = "year"
INTERIM = "interim"
QUARTER_END = "quarter_end"
YEAR_END = "year_end"
INTERIM_END = "interim_end"

PERIOD_KINDS: tuple[str, ...] = (YTD, QUARTER, YEAR, INTERIM, QUARTER_END, YEAR_END, INTERIM_END)

_CONTEXT_KIND = re.compile(r"(YTD|Quarter|Year|Interim)(Duration|Instant)$")

_KIND_BY_CONTEXT: dict[tuple[str, str], str] = {
    ("YTD", "Duration"): YTD,
    ("Quarter", "Duration"): QUARTER,
    ("Year", "Duration"): YEAR,
    ("Interim", "Duration"): INTERIM,
    ("Quarter", "Instant"): QUARTER_END,
    ("Year", "Instant"): YEAR_END,
    ("Interim", "Instant"): INTERIM_END,
}
"""context の末尾から期の種類を決める.

``CurrentYTDDuration`` は年初来累計、``Prior1YearInstant`` は前期末の時点になる。
``Current`` / ``Prior1`` / ``Prior2`` の別は ``period_end`` を見れば分かるので持たない。

``FilingDateInstant`` はここに載せない。提出日時点の株式数などが入るもので、財務諸表の
項目ではない。実測 4,142 書類すべてに出るが、拾っても使い道が無い。
"""


@dataclass(frozen=True, slots=True)
class SourceFact:
    """名寄せの入力. ``tdnet_statement_facts`` の 1 行から要る列だけを取ったもの."""

    section: str
    concept: str
    context_ref: str
    period_type: str
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None


@dataclass(frozen=True, slots=True)
class FinancialValue:
    """名寄せの結果 1 つ."""

    item: str
    period_kind: str
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None
    source_section: str
    source_concept: str


def normalize(facts: Iterable[SourceFact]) -> list[FinancialValue]:
    """添付のファクトを財務項目に寄せる. 項目の定義順・期の昇順で返す.

    連結と単体は呼び出し側で絞ること。添付はファイルで分かれており、``is_consolidated``
    で選べる。両方を渡すと同じ期に 2 つの候補が並んで取り違える。

    セグメント別の値も渡さないこと (``member IS NULL`` で絞る)。
    """
    rows = list(facts)
    results: list[FinancialValue] = []
    for spec in ITEM_SPECS:
        results.extend(_normalize_item(spec, rows))
    return results


def period_kind_of(context_ref: str) -> str | None:
    """context から期の種類を読む. 財務諸表の項目でなければ None.

    最初の ``_`` より前だけを見る。単体決算の会社は
    ``CurrentQuarterInstant_NonConsolidatedMember`` の形になり、期の種類は前半にある。
    末尾で合わせると、この会社の数値が丸ごと落ちる。
    """
    head, _, _rest = context_ref.partition("_")
    matched = _CONTEXT_KIND.search(head)
    if matched is None:
        return None
    return _KIND_BY_CONTEXT.get((matched.group(1), matched.group(2)))


def _normalize_item(spec: ItemSpec, facts: Sequence[SourceFact]) -> list[FinancialValue]:
    """1 項目を (期の種類, 期末) ごとに 1 つへ絞る.

    有報と同じく ``sources`` の並びが優先順になる。短信に経営指標の推移 (BR) は無いので、
    実際に当たるのは PL と BS のエントリだけになる。BR のエントリが残っていても当たらない
    だけで害は無い。
    """
    priority = {source: index for index, source in enumerate(spec.sources)}
    best: dict[tuple[str, date], tuple[int, SourceFact]] = {}

    for fact in facts:
        if fact.period_type != spec.period_type:
            continue
        rank = priority.get((fact.section, fact.concept))
        if rank is None:
            continue
        kind = period_kind_of(fact.context_ref)
        if kind is None:
            continue

        key = (kind, fact.period_end)
        current = best.get(key)
        if current is None or rank < current[0]:
            best[key] = (rank, fact)

    return [
        FinancialValue(
            item=spec.item,
            period_kind=kind,
            period_start=fact.period_start,
            period_end=period_end,
            value=fact.value,
            unit=fact.unit,
            source_section=fact.section,
            source_concept=fact.concept,
        )
        for (kind, period_end), (_, fact) in sorted(best.items())
    ]
