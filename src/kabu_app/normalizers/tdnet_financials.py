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

``period_kind`` は context の最初の ``_`` より前だけを見る。単体決算の会社は
``CurrentQuarterInstant_NonConsolidatedMember`` の形になり、末尾で合わせると 763 書類が
丸ごと落ちる。

800 件で試して 1 項目も取れない開示は 2 件 (0.25%) だった。名寄せた売上高を表紙の実績と
突き合わせると 656 件中 655 件 (99.8%) が一致し、残る 1 件は会社側の記載ミスで訂正短信が
出ているものだった。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from kabu_app.normalizers.financials import (
    ITEM_SPECS,
    NET_ASSETS,
    NET_INCOME,
    NET_SALES,
    OPERATING_INCOME,
    ORDINARY_INCOME,
    TOTAL_ASSETS,
    ItemSpec,
)

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


_SUMMARY_PERIOD_KIND: dict[tuple[str, str], str] = {
    ("Year", "Duration"): YEAR,
    ("Year", "Instant"): YEAR_END,
    ("AccumulatedQ1", "Duration"): YTD,
    ("AccumulatedQ2", "Duration"): YTD,
    ("AccumulatedQ3", "Duration"): YTD,
    ("AccumulatedQ1", "Instant"): QUARTER_END,
    ("AccumulatedQ2", "Instant"): QUARTER_END,
    ("AccumulatedQ3", "Instant"): QUARTER_END,
}
"""表紙の期の呼び方を、添付と同じ period_kind に移す.

表紙は四半期の連番でしか期を表さない。添付の context にある ``Interim`` にあたるものが
無いため、中間決算短信も ``AccumulatedQ2`` で来る。中間期を ``interim`` と区別できないので
``ytd`` に寄せる。6 か月累計は年初来累計そのものなので、値の意味は変わらない。様式の
区別が付かなくなるだけになる。

単独四半期 (``quarter``) は表紙に無い。表紙が載せるのは累計だけになる。
"""


@dataclass(frozen=True, slots=True)
class SummarySourceFact:
    """表紙の名寄せの入力. ``tdnet_summary_facts`` の 1 行から要る列だけを取ったもの."""

    concept: str
    period_kind: str
    period_type: str
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None


@dataclass(frozen=True, slots=True)
class SummarySpec:
    """表紙から 1 項目を拾う定義.

    ``concepts`` は優先順の高い順。IFRS・米国基準・日本基準の順に並べる。会計基準を
    切り替えた期に両方の要素が入ることがあり、新しい基準を採る。
    """

    item: str
    period_type: str
    concepts: tuple[str, ...]


SUMMARY_SPECS: tuple[SummarySpec, ...] = (
    SummarySpec(
        item=NET_SALES,
        period_type="duration",
        concepts=(
            "tse-ed-t_NetSalesIFRS",
            "tse-ed-t_RevenueIFRS",
            "tse-ed-t_NetSalesUS",
            # 野村・オリックスの収益。金融費用控除後を先に置く
            "tse-ed-t_TotalRevenuesAfterDeductingFinancialExpenseUS",
            "tse-ed-t_TotalRevenuesUS",
            "tse-ed-t_OperatingRevenuesUS",
            "tse-ed-t_NetSales",
            "tse-ed-t_OperatingRevenues",
            # 銀行の経常収益と証券の営業収益。要素名の Income に釣られないこと
            "tse-ed-t_OrdinaryRevenuesBK",
            "tse-ed-t_NetOperatingRevenuesSE",
            "tse-ed-t_OperatingRevenuesSE",
        ),
    ),
    SummarySpec(
        item=OPERATING_INCOME,
        period_type="duration",
        concepts=(
            "tse-ed-t_OperatingIncomeIFRS",
            "tse-ed-t_OperatingIncomeUS",
            "tse-ed-t_OperatingIncome",
        ),
    ),
    SummarySpec(
        item=ORDINARY_INCOME,
        period_type="duration",
        # IFRS と米国基準に経常利益の概念は無い。税引前利益で代える
        concepts=(
            "tse-ed-t_ProfitBeforeTaxIFRS",
            "tse-ed-t_IncomeBeforeIncomeTaxesUS",
            "tse-ed-t_IncomeFromContinuingOperationsBeforeIncomeTaxesUS",
            "tse-ed-t_OrdinaryIncome",
        ),
    ),
    SummarySpec(
        item=NET_INCOME,
        period_type="duration",
        # 親会社帰属を優先する。単体決算の会社だけ全体の当期純利益に落ちる
        concepts=(
            "tse-ed-t_ProfitAttributableToOwnersOfParentIFRS",
            "tse-ed-t_ProfitIFRS",
            "tse-ed-t_NetIncomeUS",
            "tse-ed-t_ProfitAttributableToOwnersOfParent",
            "tse-ed-t_NetIncome",
        ),
    ),
    SummarySpec(
        item=TOTAL_ASSETS,
        period_type="instant",
        concepts=(
            "tse-ed-t_TotalAssetsIFRS",
            "tse-ed-t_TotalAssetsUS",
            "tse-ed-t_TotalAssets",
        ),
    ),
    SummarySpec(
        item=NET_ASSETS,
        period_type="instant",
        # 自己資本 (OwnersEquity / ShareholdersEquityUS /
        # EquityAttributableToOwnersOfParentIFRS) は純資産ではないので入れない
        concepts=(
            "tse-ed-t_TotalEquityIFRS",
            "tse-ed-t_NetAssetsUS",
            "tse-ed-t_NetAssets",
        ),
    ),
)
"""表紙から 6 項目を拾う定義.

添付が空の書類でだけ使う。米国基準の会社は決算短信の添付 XBRL を出しておらず、実測では
41 書類すべてで財務数値が 1 つも入っていなかった。数値データの訂正短信も表紙だけを出す。

表紙の値は百万円に丸めてある。添付があるならそちらが正確なので、必ず添付を先に見ること。
"""


def normalize_summary(facts: Iterable[SummarySourceFact]) -> list[FinancialValue]:
    """表紙のファクトを財務項目に寄せる. 項目の定義順・期の昇順で返す.

    会社予想 (``fact_type`` が Forecast) は呼び出し側で外すこと。表紙には当期の予想が
    同じ要素名で載るので、混ぜると実績と区別が付かなくなる。

    連結と単体も呼び出し側で絞ること。添付と同じ扱いになる。
    """
    rows = list(facts)
    results: list[FinancialValue] = []
    for spec in SUMMARY_SPECS:
        results.extend(_normalize_summary_item(spec, rows))
    return results


def _normalize_summary_item(
    spec: SummarySpec, facts: Sequence[SummarySourceFact]
) -> list[FinancialValue]:
    """表紙から 1 項目を (期の種類, 期末) ごとに 1 つへ絞る."""
    priority = {concept: index for index, concept in enumerate(spec.concepts)}
    best: dict[tuple[str, date], tuple[int, SummarySourceFact]] = {}

    for fact in facts:
        if fact.period_type != spec.period_type:
            continue
        rank = priority.get(fact.concept)
        if rank is None:
            continue
        kind = _SUMMARY_PERIOD_KIND.get((fact.period_kind, fact.period_type.capitalize()))
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
            source_section="SM",
            source_concept=fact.concept,
        )
        for (kind, period_end), (_, fact) in sorted(best.items())
    ]
