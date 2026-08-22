"""有価証券報告書の XBRL から財務諸表の数値を取り出す.

ZIP 1 本を読んでファクトの列に落とす。DB も外部通信も知らない。

XBRL は 3 つの情報が別々の場所にある。勘定の並びと階層は PRE (表示リンクベース)、
期間と区分は インスタンスの ``context``、値はインスタンスの要素本体にある。この 3 つを
突き合わせて 1 行 1 数値に均すのがこのモジュールの仕事になる。

対象は財務諸表の 5 セクションだけに絞る (:data:`SECTIONS`)。株主資本等変動計算書・
包括利益計算書・注記は取らない。範囲を広げるときは :data:`_CONSOLIDATED_ROLES` と
:data:`_STANDALONE_ROLES` に role を足す。
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lxml import etree

from kabu_app.parsers.linkbase import (
    LINK_NS,
    XLINK_FROM,
    XLINK_HREF,
    XLINK_LABEL,
    XLINK_ROLE,
    XLINK_TO,
    concept_of,
    read_labels,
)

logger = logging.getLogger(__name__)

SECTIONS = ("BR", "BR_C", "BS", "PL", "CS")
"""取り出すセクション.

- ``BR``: 主要な経営指標等の推移。連結。5 期分が並ぶ
- ``BR_C``: 同じく提出会社のもの。連結会社でも単体の推移がここに出る
- ``BS``: 貸借対照表
- ``PL``: 損益計算書
- ``CS``: キャッシュ・フロー計算書
"""

_PUBLIC_DOC = "PublicDoc"
"""監査報告書などを除いた本文の入るディレクトリ。ここ以外の XBRL は見ない."""

_XBRLI_NS = {"xbrli": "http://www.xbrl.org/2003/instance"}

_CONSOLIDATED_ROLES: dict[str, tuple[str, int]] = {
    # 日本 GAAP の連結。最優先
    "rol_BusinessResultsOfGroup": ("BR", 1),
    "rol_BusinessResultsOfReportingCompany": ("BR_C", 1),
    "rol_ConsolidatedBalanceSheet": ("BS", 1),
    "rol_ConsolidatedStatementOfIncome": ("PL", 1),
    "rol_ConsolidatedStatementOfCashFlows-indirect": ("CS", 1),
    # IFRS の連結
    "rol_ConsolidatedStatementOfFinancialPositionIFRS": ("BS", 2),
    "rol_ConsolidatedStatementOfProfitOrLossIFRS": ("PL", 2),
    "rol_ConsolidatedStatementOfCashFlowsIFRS": ("CS", 2),
    # 単体の財務諸表。連結の role が無いときだけ拾う
    "rol_BalanceSheet": ("BS", 3),
    "rol_StatementOfIncome": ("PL", 3),
    "rol_StatementOfCashFlows-indirect": ("CS", 3),
}
"""連結決算の会社で使う role とセクションの対応. 数値は優先度で、小さいほど優先する.

同じセクションに複数の role が当たることがある。IFRS 会社が日本 GAAP の role も
持っている場合などで、そのときは優先度の高い方を採る。
"""

_STANDALONE_ROLES: dict[str, tuple[str, int]] = {
    "rol_BusinessResultsOfReportingCompany": ("BR", 1),
    "rol_BalanceSheet": ("BS", 1),
    "rol_StatementOfIncome": ("PL", 1),
    "rol_StatementOfCashFlows-indirect": ("CS", 1),
}
"""単体決算の会社で使う role. 連結が無いので提出会社の推移を ``BR`` に入れる."""


class EdinetXbrlError(RuntimeError):
    """XBRL を読めなかった."""


@dataclass(frozen=True, slots=True)
class Fact:
    """財務諸表の数値 1 つ.

    ``context_ref`` をそのまま残すのが要点になる。連結全体の値とセグメント別の値は
    同じ勘定・同じ期間で並んで出てくるため、これを捨てると後から区別できなくなる。
    """

    section: str
    concept: str
    """要素名。``jppfs_cor_OperatingIncome`` の形。会社独自の拡張は EDINET コードを含む"""
    context_ref: str
    member: str | None
    """``context_ref`` から期間の部分を除いた残り。連結全体の値は None になる"""
    ordinal: int
    """計算書に刷られる順。表示リンクを深さ優先でたどった通し番号"""
    depth: int
    """階層の深さ。0 が計算書そのもので、勘定は 1 以上になる"""
    period_type: str
    """``duration`` か ``instant``"""
    period_start: date | None
    """``instant`` では None"""
    period_end: date
    value: Decimal
    unit: str | None
    decimals: str | None


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    """XBRL の DEI から取れる書類の素性."""

    sec_code: str | None
    """EDINET の証券コード。末尾 0 埋めの 5 桁のまま返す"""
    filer_name: str | None
    accounting_standard: str | None
    """``Japan GAAP`` / ``IFRS`` / ``US GAAP``"""
    is_consolidated: bool
    fiscal_year_start: date | None
    fiscal_year_end: date | None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """ZIP 1 本を読んだ結果."""

    info: DocumentInfo
    facts: tuple[Fact, ...]
    labels: dict[str, str]
    """この書類に同梱されていた要素名から日本語ラベルへの対応。会社独自の拡張を含む"""


def parse_document(zip_path: Path) -> ParsedDocument:
    """有報の ZIP を読んでファクトに落とす.

    Raises:
        EdinetXbrlError: ZIP に本文の XBRL が無い、または DEI を読めなかった場合
        zipfile.BadZipFile: ZIP が壊れている場合
        OSError: ファイルを開けない場合
    """
    with zipfile.ZipFile(zip_path, "r") as archive:
        targets = target_files(archive)
        if not targets["xbrl"]:
            raise EdinetXbrlError(f"本文の XBRL が入っていません: {zip_path}")

        instance = read_xml(archive, targets["xbrl"][0])
        info = _read_document_info(instance)

        labels: dict[str, str] = {}
        for path in targets["lab"]:
            labels.update(read_labels(read_xml(archive, path)))

        structure: dict[str, dict[str, _Line]] = {}
        for path in targets["pre"]:
            structure = _read_presentation(read_xml(archive, path), info.is_consolidated)
            if structure:
                break

    facts = _read_facts(instance, structure)
    logger.debug("%s: %d ファクト / %d ラベル", zip_path.name, len(facts), len(labels))
    return ParsedDocument(info=info, facts=facts, labels=labels)


# --- ZIP の中身の仕分け ----------------------------------------------------


def target_files(archive: zipfile.ZipFile) -> dict[str, list[str]]:
    """本文ディレクトリから、インスタンス・表示リンク・ラベルリンクを拾う."""
    found: dict[str, list[str]] = {"xbrl": [], "pre": [], "lab": []}
    for name in sorted(archive.namelist()):
        if _PUBLIC_DOC not in name:
            continue
        if name.endswith("_pre.xml"):
            found["pre"].append(name)
        elif name.endswith("_lab.xml"):
            found["lab"].append(name)
        elif name.endswith(".xbrl"):
            found["xbrl"].append(name)
    return found


def read_xml(archive: zipfile.ZipFile, name: str) -> etree._Element:
    """ZIP の中の XML を読む."""
    try:
        with archive.open(name) as handle:
            return etree.parse(handle).getroot()
    except etree.XMLSyntaxError as error:
        raise EdinetXbrlError(f"XML を読めません ({name}): {error}") from error


# --- DEI -------------------------------------------------------------------

_DEI_FIELDS = {
    "SecurityCodeDEI": "sec_code",
    "FilerNameInJapaneseDEI": "filer_name",
    "AccountingStandardsDEI": "accounting_standard",
    "WhetherConsolidatedFinancialStatementsArePreparedDEI": "is_consolidated",
    "CurrentFiscalYearStartDateDEI": "fiscal_year_start",
    "CurrentFiscalYearEndDateDEI": "fiscal_year_end",
}


def _read_document_info(instance: etree._Element) -> DocumentInfo:
    """DEI から書類の素性を読む.

    名前空間の URI では引かず要素名で拾う。jpdei の URI には版が入っており、
    タクソノミが更新されると固定した URI では取れなくなるため。
    """
    found: dict[str, str] = {}
    for element in instance.iter():
        if not isinstance(element.tag, str):
            continue
        name = etree.QName(element).localname
        field = _DEI_FIELDS.get(name)
        if field is None or field in found:
            continue
        text = (element.text or "").strip()
        if text:
            found[field] = text

    if not found:
        raise EdinetXbrlError("DEI が見つかりません")

    return DocumentInfo(
        sec_code=found.get("sec_code"),
        filer_name=found.get("filer_name"),
        accounting_standard=found.get("accounting_standard"),
        # 連結財務諸表を作らない会社だけが false を書く。無い場合は連結とみなす。
        is_consolidated=found.get("is_consolidated", "true").lower() != "false",
        fiscal_year_start=_to_date(found.get("fiscal_year_start")),
        fiscal_year_end=_to_date(found.get("fiscal_year_end")),
    )


# --- 表示リンク ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Line:
    """表示リンク上での勘定の位置."""

    ordinal: int
    depth: int


def _read_presentation(
    link_base: etree._Element, is_consolidated: bool
) -> dict[str, dict[str, _Line]]:
    """表示リンクを読んで、セクションごとに勘定の並びと階層を組み立てる.

    節点は locator ではなく**勘定**にする。同じ勘定に複数の locator が振られるため、
    locator を単位に木を作ると根が分裂して途中で切れる。有報の表示リンクでは ``Table``
    と ``LineItems`` がこの形になっていた。
    """
    roles = _CONSOLIDATED_ROLES if is_consolidated else _STANDALONE_ROLES

    sections: dict[str, dict[str, _Line]] = {}
    priorities: dict[str, int] = {}

    for presentation_link in link_base.findall(".//link:presentationLink", LINK_NS):
        role = presentation_link.get(XLINK_ROLE)
        if not role:
            continue
        matched = next(
            ((key, rank) for suffix, (key, rank) in roles.items() if role.endswith(suffix)),
            None,
        )
        if matched is None:
            continue
        section, priority = matched
        if section in priorities and priorities[section] <= priority:
            continue

        lines = _build_lines(presentation_link)
        if lines:
            sections[section] = lines
            priorities[section] = priority

    return sections


def _build_lines(presentation_link: etree._Element) -> dict[str, _Line]:
    """1 つの表示リンクを、勘定から並び順と深さを引ける形にする."""
    # locator の出現順が、そのまま計算書に刷られる順の下敷きになる
    concepts: dict[str, str] = {}
    for locator in presentation_link.findall("link:loc", LINK_NS):
        locator_id = locator.get(XLINK_LABEL)
        href = locator.get(XLINK_HREF)
        if locator_id and href:
            concepts[locator_id] = concept_of(href)

    edges: dict[str, list[tuple[float, str]]] = {}
    has_parent: set[str] = set()
    for arc in presentation_link.findall("link:presentationArc", LINK_NS):
        source = concepts.get(arc.get(XLINK_FROM) or "")
        target = concepts.get(arc.get(XLINK_TO) or "")
        if source is None or target is None or source == target:
            continue
        edges.setdefault(source, []).append((_to_float(arc.get("order")), target))
        has_parent.add(target)

    ordered = list(dict.fromkeys(concepts.values()))
    roots = [concept for concept in ordered if concept not in has_parent]
    if not roots:
        # 循環していて根が決まらない。並びは諦めて出現順に振る
        roots = ordered[:1]

    lines: dict[str, _Line] = {}
    for root in roots:
        _walk(root, 0, edges, lines, frozenset())
    # 木からたどり着けなかった勘定も落とさない。値は取れるので末尾に置く
    for concept in ordered:
        if concept not in lines:
            lines[concept] = _Line(ordinal=len(lines), depth=0)
    return lines


def _walk(
    concept: str,
    depth: int,
    edges: dict[str, list[tuple[float, str]]],
    lines: dict[str, _Line],
    ancestors: frozenset[str],
) -> None:
    """深さ優先で並び順と深さを振る.

    同じ勘定が 2 度出てきたら最初の位置を採る。キャッシュ・フロー計算書では同じ勘定が
    複数の小計にぶら下がる。値は同じなので、表示にはどちらか一方があれば足りる。
    """
    if concept in lines:
        return
    lines[concept] = _Line(ordinal=len(lines), depth=depth)
    for _, child in sorted(edges.get(concept, [])):
        if child in ancestors:
            continue
        _walk(child, depth + 1, edges, lines, ancestors | {concept})


# --- インスタンス ----------------------------------------------------------


def _read_facts(
    instance: etree._Element, structure: dict[str, dict[str, _Line]]
) -> tuple[Fact, ...]:
    """表示リンクに載っている勘定の値を、インスタンスから集める."""
    contexts = _read_contexts(instance)
    values = _read_values(instance)

    facts: dict[tuple[str, str, str], Fact] = {}
    for section, lines in structure.items():
        for concept, line in sorted(lines.items(), key=lambda item: item[1].ordinal):
            for context_ref, (text, unit, decimals) in values.get(concept, {}).items():
                period = contexts.get(context_ref)
                if period is None:
                    continue
                value = _to_decimal(text)
                # nil や空の要素。「開示はあるが数値が無い」で、分析には使えない。
                if value is None:
                    continue
                key = (section, concept, context_ref)
                if key in facts:
                    continue
                period_type, period_start, period_end = period
                facts[key] = Fact(
                    section=section,
                    concept=concept,
                    context_ref=context_ref,
                    member=_member_of(context_ref),
                    ordinal=line.ordinal,
                    depth=line.depth,
                    period_type=period_type,
                    period_start=period_start,
                    period_end=period_end,
                    value=value,
                    unit=unit,
                    decimals=decimals,
                )

    return tuple(facts.values())


def _read_contexts(instance: etree._Element) -> dict[str, tuple[str, date | None, date]]:
    """context の id から期間を引けるようにする."""
    periods: dict[str, tuple[str, date | None, date]] = {}

    for context in instance.findall("xbrli:context", _XBRLI_NS):
        context_id = context.get("id")
        period = context.find("xbrli:period", _XBRLI_NS)
        if not context_id or period is None:
            continue

        instant = _text_of(period.find("xbrli:instant", _XBRLI_NS))
        if instant is not None:
            end = _to_date(instant)
            if end is not None:
                periods[context_id] = ("instant", None, end)
            continue

        start = _to_date(_text_of(period.find("xbrli:startDate", _XBRLI_NS)))
        end = _to_date(_text_of(period.find("xbrli:endDate", _XBRLI_NS)))
        if end is not None:
            periods[context_id] = ("duration", start, end)

    return periods


def _read_values(
    instance: etree._Element,
) -> dict[str, dict[str, tuple[str | None, str | None, str | None]]]:
    """インスタンスの要素を、要素名と context の組で引けるようにする.

    表示リンクの側から 1 勘定ずつ ``findall`` すると、勘定の数だけ木を舐めることになる。
    1 度で集めてから引く。
    """
    values: dict[str, dict[str, tuple[str | None, str | None, str | None]]] = {}

    for element in instance.iter():
        if not isinstance(element.tag, str):
            continue
        context_ref = element.get("contextRef")
        if not context_ref:
            continue
        qname = etree.QName(element)
        prefix = _prefix_of(instance, qname.namespace)
        if prefix is None:
            continue
        concept = f"{prefix}_{qname.localname}"
        values.setdefault(concept, {})[context_ref] = (
            element.text,
            element.get("unitRef"),
            element.get("decimals"),
        )

    return values


def _prefix_of(instance: etree._Element, namespace: str | None) -> str | None:
    """名前空間の URI を、その文書で使われている接頭辞に戻す.

    要素名は接頭辞込みで持つ。表示リンクの href が ``jppfs_cor_NetSales`` の形で
    要素を指しており、これに合わせないと突き合わせられない。
    """
    if namespace is None:
        return None
    for prefix, uri in instance.nsmap.items():
        if uri == namespace and prefix:
            return str(prefix)
    return None


# --- 小物 ------------------------------------------------------------------


def _member_of(context_ref: str) -> str | None:
    """context の id から期間の部分を落として member だけを返す.

    ``CurrentYearDuration`` は連結全体の値で member が無い。
    ``CurrentYearDuration_ReportableSegmentsMember`` はセグメント別になる。
    """
    _, separator, member = context_ref.partition("_")
    return member if separator else None


def _text_of(element: etree._Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _to_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _to_decimal(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None


def _to_float(value: str | None) -> float:
    """表示リンクの order。壊れていたら先頭に寄せる."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0
