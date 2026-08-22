"""EDINET のタクソノミから要素名の日本語ラベルを取り出す.

タクソノミは金融庁が年度ごとに ZIP で配る。API では取れないので手で落として置く。
置き場と入手先は README を見ること。

ラベルは表示のためだけに使う。年度をまたいで同じ要素名の文言が変わることがあるが、
新しい年度の ZIP を読み込めば上書きされる。どの年度の文言かは追わない。
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from lxml import etree

from kabu_app.parsers.edinet_xbrl import EdinetXbrlError
from kabu_app.parsers.linkbase import read_labels

logger = logging.getLogger(__name__)

TAXONOMY_NAMESPACES = ("jpcrp", "jppfs", "jpigp")
"""ラベルを読む名前空間.

- ``jpcrp``: 有報の様式そのもの。主要な経営指標等の推移がここ
- ``jppfs``: 日本 GAAP の財務諸表の勘定
- ``jpigp``: IFRS の勘定

``jpdei`` (書類の素性) と ``jpctl`` (提出処理用) は数値を持たないので読まない。
"""


def taxonomy_path(data_dir: Path, year: int) -> Path:
    """タクソノミ ZIP の置き場を返す. ``<data_dir>/edinet_taxonomy/Taxonomy_YYYY.zip``."""
    return data_dir / "edinet_taxonomy" / f"Taxonomy_{year}.zip"


def parse_taxonomy_labels(zip_path: Path) -> dict[str, str]:
    """タクソノミ ZIP を読んで、要素名から日本語ラベルへの対応を作る.

    Raises:
        EdinetXbrlError: ラベルリンクが 1 つも入っていない場合
        zipfile.BadZipFile: ZIP が壊れている場合
        OSError: ファイルを開けない場合
    """
    labels: dict[str, str] = {}

    with zipfile.ZipFile(zip_path, "r") as archive:
        targets = [name for name in sorted(archive.namelist()) if _is_label_link(name)]
        if not targets:
            raise EdinetXbrlError(f"ラベルリンクが入っていません: {zip_path}")

        for name in targets:
            with archive.open(name) as handle:
                labels.update(read_labels(etree.parse(handle).getroot()))

    logger.debug("%s: %d ファイルから %d ラベル", zip_path.name, len(targets), len(labels))
    return labels


def _is_label_link(name: str) -> bool:
    """``**/{名前空間}/**/label/*_lab.xml`` に当たるかを見る.

    タクソノミには表示リンクや定義リンクも入っている。ラベルだけを拾う。
    """
    if not name.endswith("_lab.xml"):
        return False

    parts = name.replace("\\", "/").split("/")
    if "label" not in parts:
        return False

    label_index = parts.index("label")
    return any(
        namespace in parts and parts.index(namespace) < label_index
        for namespace in TAXONOMY_NAMESPACES
    )
