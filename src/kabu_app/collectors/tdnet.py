"""TDnet の適時開示から決算短信を取得する.

このモジュールは DB を知らない。取得と正規化だけを行い、結果を dataclass で返す。

TDnet には API が無い。適時開示情報閲覧サービスの HTML を読む。一覧も実体ファイルも
31 日ほどで消えるため、取り逃した日は二度と取れない。
"""

import logging
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

import httpx
from lxml import html as lxml_html

logger = logging.getLogger(__name__)

TDNET_BASE_URL = "https://www.release.tdnet.info/inbs"

RETENTION_DAYS = 31
"""一覧と実体ファイルが残る日数。これより古い日付は取りに行っても空か 404 になる."""

REQUEST_INTERVAL = 0.5
"""連続してリクエストするときに空ける秒数。API ではなく Web ページなので長めに取る."""

_LIST_TABLE_ID = "main-list-table"
_MAX_PAGES = 100
"""ページを舐めるときの歯止め. 1 ページ 100 件なので 1 万件ぶん.

ページャは <a href> ではなく div の onClick で、10 ページを超えると番号が省略される。
総数を読むのは当てにできないので、404 か空のページに当たるまで進める。
"""


@dataclass(frozen=True, slots=True)
class TdnetDisclosureMeta:
    """開示 1 件。フィールド名は tdnet_disclosures の列名に合わせてある."""

    doc_id: str
    disclosed_date: date
    disclosed_time: time
    sec_code: str | None
    code: str
    company_name: str
    title: str
    markets: str | None
    is_amendment: bool
    xbrl_file: str | None


def fetch_disclosure_list(target_date: date, timeout: float = 30.0) -> list[TdnetDisclosureMeta]:
    """ある開示日の一覧を引き、決算短信だけを返す.

    土日祝も呼べる。開示が無い日と、31 日より前で消えた日は、どちらも空のリストになる。
    区別が要るなら呼び出し側で日付を見る。

    Raises:
        httpx.HTTPError: 通信に失敗した場合 (404 は空として扱う)
    """
    disclosures: list[TdnetDisclosureMeta] = []
    page = 0

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for page in range(1, _MAX_PAGES + 1):
            document = _fetch_page(client, target_date, page)
            if document is None:
                break
            rows, found = _parse_rows(document, target_date)
            disclosures.extend(found)
            if rows == 0:
                break

    logger.debug("%s: %d ページ / 決算短信 %d 件", target_date, page, len(disclosures))
    return disclosures


def download_file(file_name: str, dest: Path, timeout: float = 60.0) -> Path:
    """一覧に載っていたファイル名を取ってきて dest に保存する.

    途中で落ちても欠けたファイルが残らないよう、一時ファイルに書いてから移す。

    Raises:
        httpx.HTTPError: 通信に失敗した場合
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".part", delete=False)
    handle.close()
    tmp_path = Path(handle.name)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", f"{TDNET_BASE_URL}/{file_name}") as response:
                response.raise_for_status()
                with tmp_path.open("wb") as sink:
                    for chunk in response.iter_bytes():
                        sink.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    tmp_path.replace(dest)
    # 一時ファイルは 0600 で作られる。生データなので通常のファイル権限に戻す。
    dest.chmod(0o644)
    return dest


def disclosure_path(data_dir: Path, doc_id: str, disclosed_date: date, suffix: str) -> Path:
    """実体の保存先を返す. ``<data_dir>/tdnet/YYYYMMDD/{docID}.{suffix}``.

    この配置は前身の findocgen が使っていたもので、既存の 2 万件をそのまま使う。
    XBRL の ZIP も findocgen に合わせて doc_id の名前で置く。中身のファイル名とは違う。
    """
    return data_dir / "tdnet" / f"{disclosed_date:%Y%m%d}" / f"{doc_id}.{suffix}"


def is_target_disclosure(title: str) -> bool:
    """取り込む開示か判定する.

    決算短信とその訂正を対象にする。「決算短信の発表日変更のお知らせ」のような、
    短信そのものではない開示も表題に「決算短信」を含むが、これらは XBRL を持たない。
    捨てずに残し、実体を落とすかどうかは XBRL の有無で決める。
    """
    return "決算短信" in title


def to_stock_code(sec_code: str) -> str:
    """TDnet の証券コードを JPX の銘柄コードに変換する.

    EDINET と同じく 5 桁で末尾を 0 埋めする。``252A0`` は JPX の ``252A``。
    """
    if len(sec_code) == 5 and sec_code.endswith("0"):
        return sec_code[:4]
    return sec_code


def _fetch_page(client: httpx.Client, target_date: date, page: int) -> lxml_html.HtmlElement | None:
    """1 ページ読む. 存在しなければ None."""
    url = f"{TDNET_BASE_URL}/I_list_{page:03d}_{target_date:%Y%m%d}.html"
    response = client.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    # charset を書いていないページなので、lxml の推測に任せず UTF-8 で読む。
    return lxml_html.fromstring(response.content.decode("utf-8", errors="replace"))


def _parse_rows(
    document: lxml_html.HtmlElement, target_date: date
) -> tuple[int, list[TdnetDisclosureMeta]]:
    """一覧のテーブルから決算短信の行を抜く. 戻りは (テーブルの行数, 決算短信).

    行数を返すのは、次のページがあるかの判断に使うため。決算短信が 0 件でも、
    他の開示が並んでいればページは続く。
    """
    tables = document.xpath(f"//table[@id='{_LIST_TABLE_ID}']")
    if not tables:
        return 0, []

    rows = tables[0].xpath(".//tr")
    disclosures = [
        meta for row in rows if (meta := _build_meta(row.xpath("./td"), target_date)) is not None
    ]
    return len(rows), disclosures


def _build_meta(
    cells: list[lxml_html.HtmlElement], target_date: date
) -> TdnetDisclosureMeta | None:
    """行のセルを正規化する. 対象外なら None を返す.

    列は 7 つ。時刻・証券コード・会社名・表題 (PDF リンク)・XBRL リンク・上場市場・更新履歴。
    """
    if len(cells) < 6:
        return None

    links = cells[3].xpath(".//a")
    if not links:
        return None
    title = _text(links[0])
    if not is_target_disclosure(title):
        return None

    pdf_file = str(links[0].get("href", "")).strip()
    if not pdf_file.endswith(".pdf"):
        return None

    sec_code = _text(cells[1])
    if not sec_code:
        return None

    xbrl_links = cells[4].xpath(".//a/@href")
    xbrl_file = next((str(href).strip() for href in xbrl_links if str(href).endswith(".zip")), None)

    return TdnetDisclosureMeta(
        doc_id=pdf_file[: -len(".pdf")],
        disclosed_date=target_date,
        disclosed_time=_parse_time(_text(cells[0])),
        sec_code=sec_code,
        code=to_stock_code(sec_code),
        company_name=_text(cells[2]),
        title=title,
        markets=_text(cells[5]),
        is_amendment="訂正" in title,
        xbrl_file=xbrl_file,
    )


def _text(element: lxml_html.HtmlElement) -> str:
    return str(element.text_content()).strip()


def _parse_time(value: str) -> time:
    """開示時刻は "HH:MM"."""
    return datetime.strptime(value, "%H:%M").time()
