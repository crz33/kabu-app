"""有報の「大株主の状況」から上位株主を取り出し、オーナー系かどうかを判定する.

株主名は文字列でしか手に入らない。「オーナー家が握っているか」を機械的に測るには、
信託口のような名義人と、創業家の資産管理会社とを名前から見分けるしかない。ここでの
判定はその近似で、外れる例は残る。判定の根拠が追えるよう ``kind`` を必ず残す。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lxml import etree

from kabu_app.parsers.edinet_xbrl import EdinetXbrlError, read_xml, target_files

_MAJOR_SHAREHOLDER_CONTEXT = re.compile(r"^CurrentYearInstant_No(\d+)MajorShareholdersMember$")
"""大株主 1 行分を指す context。当期のものだけを採る."""

_TAG_NAME = "NameMajorShareholders"
_TAG_SHARES = "NumberOfSharesHeld"
_TAG_RATIO = "ShareholdingRatio"
_TAG_DIRECTOR = "NameInformationAboutDirectorsAndCorporateAuditors"

OWNER_COMPANY_THRESHOLD = Decimal("30.0")
"""オーナー系の持ち分がこの割合 (%) 以上なら「オーナー企業」とみなす."""

_TRUST_KEYWORDS = (
    "信託銀行",
    "信託口",
    "カストディ",
    "マスタートラスト",
    "日本カストディ",
    "資産管理サービス信託",
    "ステート・ストリート",
    "ステートストリート",
    "バンク・オブ・ニューヨーク",
    "ＪＰモルガン",
    "JPモルガン",
    "証券口",
)
"""信託口・カストディ。名義人でしかなく、実質の持ち主は別にいる."""

_PUBLIC_KEYWORDS = ("財務大臣", "大臣", "地方公共団体", "日本政府", "都道府県")
"""国や自治体の保有。安定株主だがオーナー家ではない."""

_EMPLOYEE_KEYWORDS = ("持株会", "持ち株会")
"""従業員・取引先持株会。同じく安定株主だがオーナー家ではない."""

_FINANCIAL_KEYWORDS = (
    "銀行",
    "証券",
    "保険",
    "生命",
    "信用金庫",
    "信用組合",
    "投信",
    "アセットマネジメント",
)
"""金融機関。政策保有の株主で、役員と同姓でもオーナー家のビークルではない."""

_ASSET_MGMT_KEYWORDS = ("資産管理", "有限会社", "（有）", "(有)", "㈲")
"""オーナー家の資産保有会社を示す語.

「商事」「ホールディングス」は大手事業会社も使うので入れない。この形の会社は
役員の姓との一致で拾う。
"""

_CORPORATE_KEYWORDS = (
    "株式会社",
    "有限会社",
    "合同会社",
    "合資会社",
    "合名会社",
    "相互会社",
    "（株）",
    "(株)",
    "（有）",
    "(有)",
    "（相）",
    "(相)",
    "㈱",
    "㈲",
    "㈳",
    "㈴",
    "㈵",
    "㈶",
    "財団",
    "社団",
    "組合",
    "機構",
    "基金",
    "銀行",
    "信託",
    "証券",
    "保険",
    "生命",
    "ファンド",
    "持株会",
    "共済会",
    "Co",
    "Ltd",
    "Inc",
    "Corp",
    "LLC",
    "L.P",
    "LP",
    "FUND",
    "BANK",
    "TRUST",
    "SECS",
    "SECURITIES",
    "N.A",
)
"""これを含めば法人。裏を返すと、含まなければ個人とみなす."""

KINDS = ("individual", "director", "asset_mgmt", "employee", "trust", "public", "corporate")
"""``Shareholder.kind`` に入る値.

- ``individual``: 個人
- ``director``: 役員本人、または役員名と一致する法人
- ``asset_mgmt``: オーナー家の資産管理会社
- ``employee``: 従業員・取引先持株会
- ``trust``: 信託口・カストディ
- ``public``: 国・自治体
- ``corporate``: それ以外の法人
"""


@dataclass(frozen=True, slots=True)
class Shareholder:
    """大株主 1 名."""

    rank: int
    name: str
    shares: int | None
    ratio: Decimal | None
    """発行済株式総数に対する保有割合 (%)"""
    kind: str
    is_owner: bool


@dataclass(frozen=True, slots=True)
class ShareholdersResult:
    """1 社分の抽出結果."""

    shareholders: tuple[Shareholder, ...] = ()
    directors: tuple[str, ...] = field(default=())

    @property
    def owner_ratio(self) -> Decimal | None:
        """オーナー系株主の合算保有割合 (%). 1 件も取れなければ None."""
        if not self.shareholders:
            return None
        return sum(
            (h.ratio for h in self.shareholders if h.is_owner and h.ratio is not None),
            start=Decimal(0),
        )

    @property
    def top_ratio(self) -> Decimal | None:
        """抽出できた大株主の合算保有割合 (%). 有報は上位 10 名までしか載せない."""
        if not self.shareholders:
            return None
        return sum((h.ratio for h in self.shareholders if h.ratio is not None), start=Decimal(0))

    @property
    def is_owner_company(self) -> bool | None:
        """オーナー企業とみなせるか."""
        ratio = self.owner_ratio
        return None if ratio is None else ratio >= OWNER_COMPANY_THRESHOLD


def parse_shareholders(zip_path: Path) -> ShareholdersResult:
    """有報の ZIP から当期の大株主と役員名を取り出す.

    大株主の記載が無い書類では空の結果を返す。持株会社の一部や、上場したばかりで
    様式が揃っていない会社にある。

    Raises:
        EdinetXbrlError: ZIP に本文の XBRL が無い場合
        zipfile.BadZipFile: ZIP が壊れている場合
        OSError: ファイルを開けない場合
    """
    with zipfile.ZipFile(zip_path, "r") as archive:
        targets = target_files(archive)
        if not targets["xbrl"]:
            raise EdinetXbrlError(f"本文の XBRL が入っていません: {zip_path}")
        instance = read_xml(archive, targets["xbrl"][0])

    rows: dict[int, dict[str, str]] = {}
    directors: list[str] = []

    for element in instance.iter():
        if not isinstance(element.tag, str):
            continue
        tag = etree.QName(element).localname
        text = (element.text or "").strip()

        if tag == _TAG_DIRECTOR:
            if text:
                directors.append(text)
            continue
        if tag not in (_TAG_NAME, _TAG_SHARES, _TAG_RATIO):
            continue

        matched = _MAJOR_SHAREHOLDER_CONTEXT.match(element.get("contextRef") or "")
        if matched is None:
            continue
        rows.setdefault(int(matched.group(1)), {})[tag] = text

    director_names = {_normalize(name) for name in directors if name}
    director_surnames = _surnames(directors)

    shareholders = []
    for rank in sorted(rows):
        row = rows[rank]
        name = row.get(_TAG_NAME, "").strip()
        if not name:
            continue
        kind, is_owner = classify(name, director_names, director_surnames)
        shareholders.append(
            Shareholder(
                rank=rank,
                name=name,
                shares=_to_int(row.get(_TAG_SHARES)),
                ratio=_to_percent(row.get(_TAG_RATIO)),
                kind=kind,
                is_owner=is_owner,
            )
        )

    return ShareholdersResult(shareholders=tuple(shareholders), directors=tuple(directors))


def classify(name: str, director_names: set[str], director_surnames: set[str]) -> tuple[str, bool]:
    """株主名を分類し、オーナー系かどうかを返す.

    オーナー系は「個人」「役員名と一致するもの」「資産管理会社」の 3 つ。資産管理会社は
    「資産管理」「有限会社」等の語か、法人名に役員の姓を含むかで拾う。後者は公益財団法人
    「河内」奨学財団のような一族のビークルを狙っている。

    名義人と公的保有を先に外す。三井住友信託銀行のような名前は、役員の姓と一致しても
    オーナー家のビークルではない。
    """
    normalized = _normalize(name)

    if _has_keyword(name, _PUBLIC_KEYWORDS):
        return "public", False
    if _has_keyword(name, _TRUST_KEYWORDS):
        return "trust", False
    if _has_keyword(name, _EMPLOYEE_KEYWORDS):
        return "employee", False

    is_director = normalized in director_names
    if not _has_keyword(name, _CORPORATE_KEYWORDS):
        return ("director" if is_director else "individual"), True

    # 金融機関は姓の一致から外す。松井証券のように、たまたま役員と同姓の独立した会社を
    # 一族のビークルと取り違える。政策保有は割合が大きく、owner_ratio に効いてしまう。
    has_surname = not _has_keyword(name, _FINANCIAL_KEYWORDS) and any(
        surname in normalized for surname in director_surnames
    )
    if _has_keyword(name, _ASSET_MGMT_KEYWORDS) or has_surname:
        return "asset_mgmt", True
    return ("director", True) if is_director else ("corporate", False)


def _has_keyword(name: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in name for keyword in keywords)


def _normalize(name: str) -> str:
    """突き合わせ用に空白と記号を落として大文字に揃える."""
    return re.sub(r"[\s　・,，.。]", "", name.replace("　", "")).upper()


def _surnames(directors: list[str]) -> set[str]:
    """役員名の先頭 2〜3 文字を姓の候補として集める.

    空白では切れない。有報の役員名は「南 部　真 希 也」のように 1 文字ずつ空けて均等
    割り付けする書き方が多く、空白で分けると「南」しか残らない。空白を落としてから頭を
    取る。日本人の姓はほぼ 2 文字か 3 文字なので、両方を候補にする。

    3 文字の候補には「南部靖」のような姓と名にまたがる文字列も混ざる。株主名にこの形が
    含まれることはまず無いので、そのままにしてある。
    """
    found = set()
    for director in directors:
        normalized = _normalize(director)
        if len(normalized) < 3:
            # 姓と名を合わせて 2 文字では、どこまでが姓か決められない
            continue
        found.add(normalized[:2])
        if len(normalized) >= 4:
            found.add(normalized[:3])
    return found


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    stripped = re.sub(r"[,\s　]", "", text)
    try:
        return int(Decimal(stripped))
    except (InvalidOperation, ValueError):
        return None


def _to_percent(text: str | None) -> Decimal | None:
    """保有割合を % に直す. XBRL には小数で入っており 0.1164 は 11.64% を指す."""
    if text is None:
        return None
    try:
        return (Decimal(text.strip()) * 100).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
