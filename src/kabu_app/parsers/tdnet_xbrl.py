"""決算短信の iXBRL から数値を取り出す.

ZIP 1 本を読んでファクトの列に落とす。DB も外部通信も知らない。

有報とは 3 つ違う。値が HTML に埋まった iXBRL で、``.xbrl`` のインスタンスが無い。ZIP に
2 系統が入っており、体系がまったく別になる。そして表紙に**会社予想**が載る。

- ``XBRLData/Summary/`` は表紙の業績ハイライト。``tse-ed-t`` の独自体系で、実績・会社予想・
  予想レンジの上下限が並ぶ。予想はここにしか無い
- ``XBRLData/Attachment/`` は財務諸表の本体。``jppfs_cor`` / ``jpigp_cor`` と有報と同じ体系で、
  内訳と精度がある。予想は入らない

実績は Attachment を使う。Summary は ``scale="6"`` で百万円に丸めてあり、同じ売上高が
Summary で 138,877 (百万円)、Attachment で 138,877,139 (円) になる。Summary を読むのは
予想のためだけになる。
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lxml import etree

logger = logging.getLogger(__name__)

SUMMARY_DIR = "/Summary/"
ATTACHMENT_DIR = "/Attachment/"

STATEMENTS: dict[str, str] = {
    "bs": "BS",
    "fs": "BS",
    "pl": "PL",
    "pc": "PC",
    "ci": "CI",
    "cf": "CF",
    "ss": "SS",
    "sg": "SG",
}
"""Attachment のファイル名に入る計算書コードと、こちらで使う略号.

``0101010-acbs01-tse-acedjpfr-89280-...-ixbrl.htm`` の ``acbs`` が該当する。1 文字目が期
(a=通期 / q=四半期 / s=中間)、2 文字目が連結区分 (c=連結 / n=単体)、続く 2 文字が計算書。

- ``BS``: 貸借対照表。IFRS は ``fs`` (財政状態計算書) で出すので同じ略号に寄せる
- ``PL``: 損益計算書
- ``PC``: ``pc`` で出る損益計算書。実測 200 書類で 2 件だけ、番号も 0600000 番台と他と違う。
  中身は通常の PL と同じ要素なので、混ぜると主キーがぶつかる。別に置いて後から判断する
- ``CI``: 包括利益計算書
- ``CF``: キャッシュ・フロー計算書
- ``SS``: 株主資本等変動計算書
- ``SG``: セグメント情報

``np`` と ``nb`` は載せない。実測ではどちらも数値を 1 つも持たず、注記の本文だけだった。
"""

_ATTACHMENT_NAME = re.compile(r"-([aqs])([cn])(bs|fs|pl|pc|ci|cf|ss|sg)\d*-")
"""Attachment のファイル名から期・連結区分・計算書を読む."""

_SUMMARY_CONTEXT = re.compile(
    r"^(?P<scope>Current|Prior|Next)"
    r"(?P<kind>AccumulatedQ[1-4]|Year|Quarter|Interim|YTD)"
    r"(?P<period>Duration|Instant)"
    r"(?P<rest>(?:_\w+?Member)*)$"
)
"""Summary の context を 4 つの軸に割る.

``NextYearDuration_ConsolidatedMember_ForecastMember`` の形になる。末尾の Member の並びは
書類によって数が変わるので、まとめて受けてから後ろ 2 つを見る。
"""

_CONSOLIDATION_MEMBERS = {"Consolidated": True, "NonConsolidated": False}

FACT_TYPES = ("Result", "Forecast", "Upper", "Lower")
"""Summary の値の種別.

``Upper`` と ``Lower`` は予想を幅で出す会社の上下限になる。実測 60 書類では
Upper 876 + Lower 876 に対して Forecast が 1,130 で、レンジのほうが多い。落とすと
その会社の予想が丸ごと消える。
"""

_IX_NONFRACTION = "nonfraction"
_IX_NONNUMERIC = "nonnumeric"

_STANDARDS = (
    ("ＩＦＲＳ", "IFRS"),
    ("IFRS", "IFRS"),
    ("米国基準", "US GAAP"),
    ("ＵＳ", "US GAAP"),
    ("日本基準", "Japan GAAP"),
)
"""表題に入る会計基準の表記. 全角と半角が混ざるので順に当てる."""

_QUARTER_WORDS = (
    ("第１四半期", "Q1"),
    ("第1四半期", "Q1"),
    ("第２四半期", "Q2"),
    ("第2四半期", "Q2"),
    ("第３四半期", "Q3"),
    ("第3四半期", "Q3"),
)


class TdnetXbrlError(Exception):
    """決算短信の iXBRL を読めなかった."""


@dataclass(frozen=True, slots=True)
class SummaryFact:
    """表紙の数値 1 つ.

    ``context_ref`` を分解した 5 つの軸を列に持つ。有報の member と違い、短信の context は
    意味が決まった軸の掛け合わせになっている。文字列のまま置くと、予想を引くたびに
    ``LIKE '%ForecastMember'`` を書くことになる。
    """

    concept: str
    """``tse-ed-t_NetSales`` の形。名前空間の区切りを有報に合わせて ``_`` にする"""
    context_ref: str
    scope: str
    """``Current`` / ``Prior`` / ``Next``"""
    period_kind: str
    """``Year`` / ``AccumulatedQ1`` 〜 ``AccumulatedQ3`` など"""
    quarter_member: str | None
    """配当の内訳に付く ``FirstQuarter`` / ``YearEnd`` / ``Annual`` など。無ければ None"""
    is_consolidated: bool | None
    fact_type: str | None
    """``Result`` / ``Forecast`` / ``Upper`` / ``Lower``。判別できなければ None"""
    period_type: str
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None
    decimals: str | None


@dataclass(frozen=True, slots=True)
class StatementFact:
    """財務諸表の数値 1 つ.

    有報の ``Fact`` に近い。``ordinal`` は iXBRL に出てくる順で、そのまま計算書に刷られた
    並びになる。有報と違って ``depth`` は持たない。階層は同梱の ``-pre.xml`` にあるが、
    計算書ごとにファイルが分かれていて出現順が既に表示順なので、まずは順番だけで足りる。
    """

    section: str
    """``BS`` / ``PL`` / ``CI`` / ``CF`` / ``SS`` / ``SG``"""
    term: str
    """``a`` (通期) / ``q`` (四半期) / ``s`` (中間)"""
    is_consolidated: bool
    concept: str
    """``jppfs_cor_NetSales`` の形"""
    context_ref: str
    member: str | None
    """context から期間の部分を除いた残り。全体の値は None"""
    ordinal: int
    period_type: str
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None
    decimals: str | None


@dataclass(frozen=True, slots=True)
class DisclosureInfo:
    """表紙から読んだ書類の素性.

    有報は DEI から取れるが、短信に DEI は無い。表題 (``tse-ed-t:DocumentName``) に会計基準と
    連結の有無が書いてある。実測 60 書類すべてに入っていた。
    """

    document_name: str | None
    sec_code: str | None
    company_name: str | None
    accounting_standard: str | None
    is_consolidated: bool | None
    fiscal_year_end: date | None
    quarter: str | None
    """``Q1`` / ``Q2`` / ``Q3`` / ``FY``"""


@dataclass(frozen=True, slots=True)
class ParsedDisclosure:
    """ZIP 1 本を読んだ結果."""

    info: DisclosureInfo
    summary_facts: tuple[SummaryFact, ...]
    statement_facts: tuple[StatementFact, ...]


def parse_disclosure(zip_path: Path) -> ParsedDisclosure:
    """決算短信の ZIP を読んでファクトに落とす.

    表紙の入らない ZIP がある。実測 400 件で 2 件あり、決算期を変えた会社の「第５四半期
    決算短信」と、非連結の中間短信だった。添付には財務諸表が入っているので捨てない。
    書類の素性は取れないので ``info`` が空になる。会計基準と四半期は
    ``tdnet_disclosures.title`` からも読めるので、要るなら呼び出し側で補うこと。

    Raises:
        TdnetXbrlError: iXBRL が 1 本も入っていない、または読めなかった場合
        zipfile.BadZipFile: ZIP が壊れている場合
        OSError: ファイルを開けない場合
    """
    with zipfile.ZipFile(zip_path, "r") as archive:
        names = sorted(archive.namelist())
        summary_names = [n for n in names if SUMMARY_DIR in n and n.endswith("-ixbrl.htm")]

        if summary_names:
            summary_root = _read_ixbrl(archive, summary_names[0])
            info = _read_disclosure_info(summary_root)
            summary_facts = _read_summary_facts(summary_root)
        else:
            info = DisclosureInfo(None, None, None, None, None, None, None)
            summary_facts = []

        # 添付は 6 本前後の iXBRL で 1 つの XBRL インスタンスを組む。context と unit の定義は
        # 先頭の 1 本 (たいてい貸借対照表) にまとめて置かれ、残りは参照するだけになる。
        # 1 本ずつ独立に読むと、2 本目から期間が引けずファクトが 1 つも取れない。
        contexts: dict[str, tuple[str, date | None, date]] = {}
        units: dict[str, str] = {}
        attachments: list[tuple[etree._Element, str, str, bool]] = []
        for name in names:
            if ATTACHMENT_DIR not in name or not name.endswith("-ixbrl.htm"):
                continue
            root = _read_ixbrl(archive, name)
            # 定義がどのファイルに入るかは決め打ちできない。計算書として読まないものからも
            # 集めておく。中身が無ければ何も増えないので、余分に見て困ることはない。
            contexts.update(_read_contexts(root))
            units.update(_read_units(root))

            matched = _ATTACHMENT_NAME.search(Path(name).name)
            if matched is None:
                # 定性情報 (qualitative.htm) や注記 (np/nb) は数値を持たない
                continue
            attachments.append(
                (root, STATEMENTS[matched.group(3)], matched.group(1), matched.group(2) == "c")
            )

        if not summary_names and not attachments:
            raise TdnetXbrlError(f"iXBRL が 1 本も入っていません: {zip_path}")

        statement_facts: list[StatementFact] = []
        for root, section, term, is_consolidated in attachments:
            statement_facts.extend(
                _read_statement_facts(
                    root,
                    contexts=contexts,
                    units=units,
                    section=section,
                    term=term,
                    is_consolidated=is_consolidated,
                    start_ordinal=len(statement_facts),
                )
            )

    logger.debug(
        "%s: 表紙 %d / 財務諸表 %d ファクト",
        zip_path.name,
        len(summary_facts),
        len(statement_facts),
    )
    return ParsedDisclosure(
        info=info,
        summary_facts=tuple(summary_facts),
        statement_facts=tuple(statement_facts),
    )


def _read_ixbrl(archive: zipfile.ZipFile, name: str) -> etree._Element:
    """ZIP の中の iXBRL を読む.

    HTML パーサで開く。iXBRL は XHTML だが、実物には閉じられていないタグが混ざる。
    厳密な XML パーサだと書類ごと落ちるので ``recover`` に頼る。

    文字コードは UTF-8 で決め打つ。パーサに任せると ``<meta charset>`` を頼りにするため、
    それが無い書類で会社名や表題が化ける。表題からは会計基準と連結の有無を読むので、
    化けると書類の素性がまるごと取れなくなる。
    """
    try:
        with archive.open(name) as handle:
            parser = etree.HTMLParser(recover=True, encoding="utf-8")
            root = etree.parse(handle, parser).getroot()
    except etree.XMLSyntaxError as error:
        raise TdnetXbrlError(f"iXBRL を読めません ({name}): {error}") from error
    if root is None:
        raise TdnetXbrlError(f"iXBRL が空です: {name}")
    return root


# --- 表紙 -------------------------------------------------------------------


def _read_disclosure_info(root: etree._Element) -> DisclosureInfo:
    """表紙から書類の素性を読む."""
    texts: dict[str, str] = {}
    for element in _iter_ix(root, _IX_NONNUMERIC):
        name = _local_name(element)
        if name and name not in texts:
            texts[name] = _text_of(element)
    for element in _iter_ix(root, _IX_NONFRACTION):
        name = _local_name(element)
        if name and name not in texts:
            texts[name] = _text_of(element)

    document_name = texts.get("DocumentName")
    return DisclosureInfo(
        document_name=document_name,
        sec_code=texts.get("SecuritiesCode") or None,
        company_name=texts.get("CompanyName") or None,
        accounting_standard=_accounting_standard(document_name),
        is_consolidated=_is_consolidated(document_name),
        fiscal_year_end=_to_date(texts.get("FiscalYearEnd")),
        quarter=_quarter(texts.get("QuarterlyPeriod"), document_name),
    )


def _accounting_standard(document_name: str | None) -> str | None:
    """表題から会計基準を読む."""
    if not document_name:
        return None
    for word, standard in _STANDARDS:
        if word in document_name:
            return standard
    return None


def _is_consolidated(document_name: str | None) -> bool | None:
    """表題から連結かどうかを読む.

    括弧は全角と半角が混ざる。「（非連結）」と「(非連結)」の両方が実在する。
    """
    if not document_name:
        return None
    if "非連結" in document_name:
        return False
    if "連結" in document_name:
        return True
    return None


def _quarter(quarterly_period: str | None, document_name: str | None) -> str | None:
    """四半期の区分を決める.

    ``QuarterlyPeriod`` を優先する。実測ではこの要素を持つ書類が 60 件中 43 件しか無く、
    通期には入らない。表題は全件にあるので、無ければそちらから読む。
    """
    if quarterly_period:
        digits = quarterly_period.strip()
        if digits in {"1", "2", "3"}:
            return f"Q{digits}"
    if document_name:
        for word, quarter in _QUARTER_WORDS:
            if word in document_name:
                return quarter
        if "決算短信" in document_name:
            return "FY"
    return None


def _read_summary_facts(root: etree._Element) -> list[SummaryFact]:
    """表紙の数値を読む."""
    periods = _read_contexts(root)
    units = _read_units(root)

    facts: list[SummaryFact] = []
    for element in _iter_ix(root, _IX_NONFRACTION):
        concept = _concept_of(element)
        context_ref = element.get("contextref")
        if concept is None or not context_ref:
            continue
        value = _numeric_value(element)
        if value is None:
            continue
        period = periods.get(context_ref)
        if period is None:
            continue
        axes = _split_summary_context(context_ref)
        if axes is None:
            continue

        period_type, period_start, period_end = period
        facts.append(
            SummaryFact(
                concept=concept,
                context_ref=context_ref,
                scope=axes[0],
                period_kind=axes[1],
                quarter_member=axes[2],
                is_consolidated=axes[3],
                fact_type=axes[4],
                period_type=period_type,
                period_start=period_start,
                period_end=period_end,
                value=value,
                unit=units.get(element.get("unitref") or ""),
                decimals=element.get("decimals"),
            )
        )
    return facts


def _split_summary_context(
    context_ref: str,
) -> tuple[str, str, str | None, bool | None, str | None] | None:
    """Summary の context を軸に割る.

    戻りは (scope, period_kind, quarter_member, is_consolidated, fact_type)。
    形が合わなければ None を返す。
    """
    matched = _SUMMARY_CONTEXT.match(context_ref)
    if matched is None:
        return None

    members = re.findall(r"_(\w+?)Member", matched.group("rest"))
    fact_type: str | None = None
    is_consolidated: bool | None = None
    quarter_member: str | None = None

    for member in members:
        if member in FACT_TYPES:
            fact_type = member
        elif member in _CONSOLIDATION_MEMBERS:
            is_consolidated = _CONSOLIDATION_MEMBERS[member]
        else:
            # 配当の内訳に付く FirstQuarter / YearEnd / Annual など
            quarter_member = member

    return (
        matched.group("scope"),
        matched.group("kind"),
        quarter_member,
        is_consolidated,
        fact_type,
    )


# --- 財務諸表 ---------------------------------------------------------------


def _read_statement_facts(
    root: etree._Element,
    contexts: dict[str, tuple[str, date | None, date]],
    units: dict[str, str],
    section: str,
    term: str,
    is_consolidated: bool,
    start_ordinal: int = 0,
) -> list[StatementFact]:
    """財務諸表 1 本の数値を読む.

    ``contexts`` と ``units`` は添付ぜんぶから集めたものを渡すこと。定義は先頭の 1 本に
    しか無く、このファイル単体からは引けない。

    ``ordinal`` は書類ぜんぶを通した番号にする。``start_ordinal`` に、ここまでに読んだ数を
    渡すこと。計算書ごとにファイルが分かれており、ファイル内の出現順が刷られた並びと
    一致する。

    書類の中で ``ordinal`` が一意になることが要る。株主資本等変動計算書は同じ勘定が表の
    セルとして何度も出るため、``concept`` と ``context_ref`` の組では重ならない保証が無い。
    """
    periods = contexts

    facts: list[StatementFact] = []
    ordinal = start_ordinal
    for element in _iter_ix(root, _IX_NONFRACTION):
        concept = _concept_of(element)
        context_ref = element.get("contextref")
        if concept is None or not context_ref:
            continue
        value = _numeric_value(element)
        if value is None:
            continue
        period = periods.get(context_ref)
        if period is None:
            continue

        ordinal += 1
        period_type, period_start, period_end = period
        facts.append(
            StatementFact(
                section=section,
                term=term,
                is_consolidated=is_consolidated,
                concept=concept,
                context_ref=context_ref,
                member=_member_of(context_ref),
                ordinal=ordinal,
                period_type=period_type,
                period_start=period_start,
                period_end=period_end,
                value=value,
                unit=units.get(element.get("unitref") or ""),
                decimals=element.get("decimals"),
            )
        )
    return facts


def _member_of(context_ref: str) -> str | None:
    """context から期間の部分を除いた残りを返す. 有報と同じ扱いにする."""
    _, separator, rest = context_ref.partition("_")
    return rest if separator else None


# --- iXBRL の読み取り -------------------------------------------------------


def _iter_ix(root: etree._Element, tag: str) -> list[etree._Element]:
    """``ix:nonFraction`` などを拾う.

    HTML パーサは名前空間を畳んで ``ix:nonfraction`` のようなタグ名にする。属性名も
    小文字になるため ``contextRef`` は ``contextref`` で引く。
    """
    return [
        element
        for element in root.iter()
        if isinstance(element.tag, str) and element.tag.rpartition(":")[2] == tag
    ]


def _concept_of(element: etree._Element) -> str | None:
    """``name`` 属性を有報と同じ ``prefix_LocalName`` の形に直す."""
    name = element.get("name")
    if not name or ":" not in name:
        return None
    prefix, _, local = name.rpartition(":")
    return f"{prefix}_{local}"


def _local_name(element: etree._Element) -> str | None:
    name = element.get("name")
    return name.rpartition(":")[2] if name else None


def _numeric_value(element: etree._Element) -> Decimal | None:
    """値を読む. ``scale`` を掛け、``sign`` が負なら反転する.

    ``scale`` は原文の桁の省略で、``6`` なら百万円単位で刷られている。掛けて円に直す。
    ``sign="-"`` は表に △ で刷られる値に付く。実測 60 書類で 488 件あり、落とすと符号が
    逆のまま入る。
    """
    text = _text_of(element)
    if not text or text in {"-", "―", "－", "△"}:
        return None
    try:
        value = Decimal(text.replace(",", "").replace("△", "-").strip())
    except InvalidOperation:
        return None

    scale = element.get("scale")
    if scale:
        try:
            value *= Decimal(10) ** int(scale)
        except (ValueError, InvalidOperation):
            return None
    if element.get("sign") == "-":
        value = -value
    return value


def _read_contexts(root: etree._Element) -> dict[str, tuple[str, date | None, date]]:
    """``xbrli:context`` から期間を読む. 戻りは (期間の種別, 開始日, 末日)."""
    contexts: dict[str, tuple[str, date | None, date]] = {}
    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag.rpartition(":")[2] != "context":
            continue
        context_id = element.get("id")
        if not context_id:
            continue

        instant: date | None = None
        start: date | None = None
        end: date | None = None
        for child in element.iter():
            if not isinstance(child.tag, str):
                continue
            local = child.tag.rpartition(":")[2]
            if local == "instant":
                instant = _to_date(_text_of(child))
            elif local == "startdate":
                start = _to_date(_text_of(child))
            elif local == "enddate":
                end = _to_date(_text_of(child))

        if instant is not None:
            contexts[context_id] = ("instant", None, instant)
        elif end is not None:
            contexts[context_id] = ("duration", start, end)
    return contexts


def _read_units(root: etree._Element) -> dict[str, str]:
    """``xbrli:unit`` から単位を読む. ``iso4217:JPY`` は ``JPY`` に均す."""
    units: dict[str, str] = {}
    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag.rpartition(":")[2] != "unit":
            continue
        unit_id = element.get("id")
        if not unit_id:
            continue
        for child in element.iter():
            if isinstance(child.tag, str) and child.tag.rpartition(":")[2] == "measure":
                text = _text_of(child)
                if text:
                    units[unit_id] = text.rpartition(":")[2]
                break
    return units


def _text_of(element: etree._Element) -> str:
    """要素の下にある文字列をつなげる.

    iXBRL の値は ``<span>`` で囲まれていることがあり、``element.text`` だけでは取れない。
    """
    return "".join(part for part in element.itertext() if isinstance(part, str)).strip()


def _to_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None
