"""XBRL のリンクベースを読む共通部分.

リンクベースは「要素どうしの関係」を書いた XML で、有報の ZIP にもタクソノミにも
同じ形で入っている。ここではラベルリンク (要素名 → 文言) だけを扱う。
"""

from __future__ import annotations

from lxml import etree

LINK_NS = {"link": "http://www.xbrl.org/2003/linkbase"}

XLINK = "http://www.w3.org/1999/xlink"
XLINK_HREF = f"{{{XLINK}}}href"
XLINK_LABEL = f"{{{XLINK}}}label"
XLINK_FROM = f"{{{XLINK}}}from"
XLINK_TO = f"{{{XLINK}}}to"
XLINK_ROLE = f"{{{XLINK}}}role"


def concept_of(href: str) -> str:
    """``...xsd#jppfs_cor_NetSales`` から要素名を取り出す."""
    return href.split("#")[-1]


def read_labels(link_base: etree._Element) -> dict[str, str]:
    """ラベルリンクから要素名と日本語ラベルの対応を作る.

    1 つの要素に複数のラベルが付く。``label`` の id が最も短いものを採ると標準ラベルに
    なる。冗長ラベルや期末用の言い換えには接尾辞が付いて長くなるため。
    """
    labels: dict[str, str] = {}

    for label_link in link_base.findall(".//link:labelLink", LINK_NS):
        texts = {
            element.get(XLINK_LABEL): (element.text or "").strip()
            for element in label_link.findall("link:label", LINK_NS)
        }
        arcs: dict[str, list[str]] = {}
        for arc in label_link.findall("link:labelArc", LINK_NS):
            source = arc.get(XLINK_FROM)
            target = arc.get(XLINK_TO)
            if source and target:
                arcs.setdefault(source, []).append(target)

        for locator in label_link.findall("link:loc", LINK_NS):
            href = locator.get(XLINK_HREF)
            locator_id = locator.get(XLINK_LABEL)
            if not href or not locator_id:
                continue
            targets = arcs.get(locator_id)
            if not targets:
                continue
            text = texts.get(min(targets, key=len))
            if text:
                labels[concept_of(href)] = text

    return labels
