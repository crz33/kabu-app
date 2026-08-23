"""有報のファクトを、会社をまたいで比べられる財務項目に名寄せする.

XBRL の要素名は会計基準と業種で割れる。売上高は日本 GAAP が ``jppfs_cor_NetSales``、
IFRS が ``jpigp_cor_RevenueIFRS``、鉄道会社は ``jppfs_cor_OperatingRevenueRWY`` になる。
このままでは会社をまたいで並べられないので、6 つの項目に寄せる。

寄せた値には必ず ``source_section`` と ``source_concept`` を付ける。金融業の経常収益を
売上高に寄せるような判断が入るため、出所を捨てると後から検証できなくなる。値がおかしい
とき、欠損なのか、名寄せの取り違えなのか、そもそも概念が違うのかを切り分けられない。

営業利益だけは経営指標の推移 (BR) から取れない。タクソノミに
``jpcrp_cor_OperatingIncomeLossSummaryOfBusinessResults`` が無く、有報の「主要な経営指標等」
にも刷られないため、損益計算書 (PL) から取る。BR 由来の項目が 5 期分そろうのに対し、
営業利益だけ 2 期分になるのはこのためで、欠損ではない。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

NET_SALES = "net_sales"
OPERATING_INCOME = "operating_income"
ORDINARY_INCOME = "ordinary_income"
NET_INCOME = "net_income"
TOTAL_ASSETS = "total_assets"
NET_ASSETS = "net_assets"


@dataclass(frozen=True, slots=True)
class SourceFact:
    """名寄せの入力. ``edinet_facts`` の 1 行から要る列だけを取ったもの.

    ``label`` はその書類での日本語ラベル。会社独自の拡張要素を拾うのに使う。
    """

    section: str
    concept: str
    period_type: str
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class FinancialValue:
    """名寄せの結果 1 つ."""

    item: str
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None
    source_section: str
    source_concept: str


@dataclass(frozen=True, slots=True)
class ItemSpec:
    """1 項目の拾い方.

    ``sources`` は (セクション, 要素名) を優先順の高い順に並べたもの。同じ期に複数の
    候補が出たら、この並びで先に来たものを採る。IFRS を日本 GAAP より前に置くのは、
    会計基準を切り替えた期に両方の要素が入ることがあるため。新しい基準を採る。

    経営指標の推移 (BR) を損益計算書 (PL)・貸借対照表 (BS) より前に置く。BR は会社が
    「主要な経営指標等」として明示的に載せた値で、5 期分そろう。PL と BS は当期と前期の
    2 期しか無い。値は実測で一致した (パソナ 2025 年 5 月期の売上高が両方 309,240 百万円)。

    提出会社の経営指標 (BR_C) は使わない。連結企業でも BR と同じ値が入る書類があり、
    連結と単体を取り違える。単体決算の会社は BR に単体の値が入るので、これで拾える。
    """

    item: str
    period_type: str
    sources: tuple[tuple[str, str], ...]
    labels: frozenset[str] = frozenset()
    """要素名で 1 つも拾えなかった期に使う日本語ラベル. 空なら使わない"""


_REVENUE_LABELS = frozenset(
    {
        "売上高",
        "売上収益",
        "営業収益",
        "収益",
        "売上高合計",
        "営業収益合計",
        "経常収益",  # 銀行・保険・金融持株会社
        "事業収益",  # 製薬・バイオ
        "完成工事高",  # 建設・エンジニアリング
        "売上高及び営業収入",  # 小売・外食
        "売上高及びその他の営業収入",
        "保険料等収入",  # 生命保険
        "営業総収入",
        "営業収入",
    }
)
"""売上高の日本語ラベル.

売上だけラベルのフォールバックを持つ。営業収益は業種ごとに要素名が分かれており、
タクソノミには鉄道 (RWY)・高速道路 (HWY)・電力 (ELC)・証券 (SEC)・投資 (INV) など
20 以上の派生がある。会社独自の拡張要素を使う会社もある。全部を要素名で並べるより、
ラベルで受けたほうが保守が続く。他の 5 項目は標準要素で足りるので持たせない。
"""

ITEM_SPECS: tuple[ItemSpec, ...] = (
    # 要素名は 4 つの会計基準ぶん並ぶ。無印が日本 GAAP、IFRS、USGAAP、JMIS (修正国際基準)。
    # JMIS の採用は数社しかないが、要素はタクソノミにあるので拾えるようにしておく。
    # KeyFinancialData は SummaryOfBusinessResults と別系統の要素で、中身は同じ経営指標になる。
    ItemSpec(
        item=NET_SALES,
        period_type="duration",
        sources=(
            ("BR", "jpcrp_cor_RevenueIFRSSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_RevenueJMISSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_NetSalesSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_RevenuesUSGAAPSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_OperatingRevenue1SummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_GrossOperatingRevenueSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_OperatingRevenue2SummaryOfBusinessResults"),
            # ラベルは「経常収益」。銀行・保険の収益で、要素名の Income に釣られないこと。
            # 実測 1,073 件はすべて銀行と金融持株会社で、みずほ FG の 9.0 兆円が入る
            ("BR", "jpcrp_cor_OrdinaryIncomeSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_RevenueKeyFinancialData"),
            ("PL", "jpigp_cor_RevenueIFRS"),
            ("PL", "jpigp_cor_NetSalesIFRS"),
            ("PL", "jppfs_cor_NetSales"),
            ("PL", "jppfs_cor_Revenue"),
            ("PL", "jppfs_cor_OperatingRevenue1"),
            # 保険業の経常収益。これもラベルを見ないと利益に見える
            ("PL", "jppfs_cor_OperatingIncomeINS"),
        ),
        labels=_REVENUE_LABELS,
    ),
    ItemSpec(
        item=OPERATING_INCOME,
        period_type="duration",
        # BR には無い。タクソノミに営業利益の SummaryOfBusinessResults 要素が存在しない
        sources=(
            ("PL", "jpigp_cor_OperatingProfitLossIFRS"),
            ("PL", "jppfs_cor_OperatingIncome"),
        ),
    ),
    ItemSpec(
        item=ORDINARY_INCOME,
        period_type="duration",
        # IFRS と US GAAP に経常利益の概念は無い。税引前利益で代える
        sources=(
            ("BR", "jpcrp_cor_OrdinaryIncomeLossSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_ProfitLossBeforeTaxIFRSSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_ProfitLossBeforeTaxJMISSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_ProfitLossBeforeTaxUSGAAPSummaryOfBusinessResults"),
            ("PL", "jppfs_cor_OrdinaryIncome"),
            ("PL", "jpigp_cor_ProfitLossBeforeTaxIFRS"),
        ),
    ),
    ItemSpec(
        item=NET_INCOME,
        period_type="duration",
        # 親会社帰属を優先する。単体決算の会社だけ全体の当期純利益に落ちる
        sources=(
            ("BR", "jpcrp_cor_ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_ProfitLossAttributableToOwnersOfParentJMISSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults"),
            (
                "BR",
                "jpcrp_cor_NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
            ),
            ("BR", "jpcrp_cor_ProfitLossIFRSSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_ProfitLossJMISSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_NetIncomeLossSummaryOfBusinessResults"),
            ("PL", "jpigp_cor_ProfitLossAttributableToOwnersOfParentIFRS"),
            ("PL", "jppfs_cor_ProfitLossAttributableToOwnersOfParent"),
            ("PL", "jpigp_cor_ProfitLossIFRS"),
            ("PL", "jppfs_cor_ProfitLoss"),
        ),
    ),
    ItemSpec(
        item=TOTAL_ASSETS,
        period_type="instant",
        sources=(
            ("BR", "jpcrp_cor_TotalAssetsIFRSSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_TotalAssetsJMISSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_TotalAssetsSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_TotalAssetsUSGAAPSummaryOfBusinessResults"),
            ("BS", "jpigp_cor_AssetsIFRS"),
            ("BS", "jppfs_cor_Assets"),
        ),
    ),
    ItemSpec(
        item=NET_ASSETS,
        period_type="instant",
        # 日本 GAAP の純資産は非支配株主持分と新株予約権を含む。IFRS の親会社所有者帰属持分
        # とは範囲が違う。自己資本が要るなら別項目として足すこと
        sources=(
            ("BR", "jpcrp_cor_EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_EquityAttributableToOwnersOfParentJMISSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_NetAssetsSummaryOfBusinessResults"),
            ("BR", "jpcrp_cor_EquityAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults"),
            (
                "BR",
                "jpcrp_cor_EquityIncludingPortionAttributableToNonControllingInterest"
                "USGAAPSummaryOfBusinessResults",
            ),
            ("BS", "jpigp_cor_EquityAttributableToOwnersOfParentIFRS"),
            ("BS", "jppfs_cor_NetAssets"),
        ),
    ),
)

ITEMS: tuple[str, ...] = tuple(spec.item for spec in ITEM_SPECS)


def normalize(facts: Iterable[SourceFact]) -> list[FinancialValue]:
    """ファクトを財務項目に寄せる. 期の昇順・項目の定義順で返す.

    連結全体の値だけを渡すこと (``member IS NULL`` で絞る)。セグメント別の値が混ざると、
    同じ期に複数の候補が並んで取り違える。
    """
    rows = list(facts)
    results: list[FinancialValue] = []
    for spec in ITEM_SPECS:
        results.extend(_normalize_item(spec, rows))
    return results


def _normalize_item(spec: ItemSpec, facts: Sequence[SourceFact]) -> list[FinancialValue]:
    priority = {source: index for index, source in enumerate(spec.sources)}
    best: dict[date, tuple[int, SourceFact]] = {}

    for fact in facts:
        if fact.period_type != spec.period_type:
            continue
        rank = priority.get((fact.section, fact.concept))
        if rank is None:
            continue
        current = best.get(fact.period_end)
        if current is None or rank < current[0]:
            best[fact.period_end] = (rank, fact)

    picked = {period_end: fact for period_end, (_, fact) in best.items()}
    for fact in _by_label(spec, facts):
        # 要素名で拾えた期はそのまま。落ちた期だけラベルで補う
        picked.setdefault(fact.period_end, fact)

    return [
        FinancialValue(
            item=spec.item,
            period_start=fact.period_start,
            period_end=fact.period_end,
            value=fact.value,
            unit=fact.unit,
            source_section=fact.section,
            source_concept=fact.concept,
        )
        for _, fact in sorted(picked.items())
    ]


def _by_label(spec: ItemSpec, facts: Sequence[SourceFact]) -> list[SourceFact]:
    """日本語ラベルで拾う. 同じ期に複数あれば大きいほうを採る.

    ラベルは前方一致も見る。XBRL のラベルには「事業収益、経営指標等」のように、読点で
    付加情報が続くものがあるため。

    同じ期に複数出るのは、内訳の行と合計の行が同じラベルを持つ場合になる。合計のほうが
    大きいので最大値を採る。
    """
    if not spec.labels:
        return []

    sections = {section for section, _ in spec.sources}
    best: dict[date, SourceFact] = {}
    for fact in facts:
        if fact.period_type != spec.period_type or fact.section not in sections:
            continue
        if not _matches_label(spec.labels, fact.label):
            continue
        current = best.get(fact.period_end)
        if current is None or fact.value > current.value:
            best[fact.period_end] = fact
    return list(best.values())


def _matches_label(labels: frozenset[str], label: str | None) -> bool:
    if label is None:
        return False
    stripped = label.strip()
    if stripped in labels:
        return True
    return any(stripped.startswith(f"{candidate}、") for candidate in labels)
