"""JPX の東証上場銘柄一覧 (data_j.xls) を取得して正規化する.

このモジュールは DB を知らない。取得と正規化だけを行い、結果を dataclass で返す。
"""

import logging
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

JPX_STOCK_LIST_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
)

MARKET_SEGMENT_BY_JPX = {
    "プライム（内国株式）": "prime",
    "スタンダード（内国株式）": "standard",
    "グロース（内国株式）": "growth",
}
"""取り込む市場・商品区分。ETF・REIT・PRO Market・外国株は対象外."""

_REQUIRED_COLUMNS = (
    "日付",
    "コード",
    "銘柄名",
    "市場・商品区分",
    "33業種コード",
    "33業種区分",
    "17業種コード",
    "17業種区分",
    "規模コード",
    "規模区分",
)

_MISSING_MARKS = frozenset({"", "-", "－"})
"""JPX は該当なしを "-" で表す。空文字と全角ハイフンも同じ扱いにする."""


@dataclass(frozen=True, slots=True)
class JpxStock:
    """銘柄 1 件。フィールド名は stocks / stock_snapshots の列名に合わせてある."""

    code: str
    name: str
    market_segment: str
    industry33_code: str
    industry33_name: str
    industry17_code: str
    industry17_name: str
    topix_scale_code: str | None
    topix_scale_name: str | None


@dataclass(frozen=True, slots=True)
class JpxStockList:
    """ある基準日の銘柄一覧."""

    base_date: date
    stocks: list[JpxStock]


def download_stock_list(dest: Path, timeout: float = 60.0) -> Path:
    """銘柄一覧の Excel をダウンロードして dest に保存する."""
    logger.info("JPX 銘柄一覧を取得: %s", JPX_STOCK_LIST_URL)
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        response = client.get(JPX_STOCK_LIST_URL)
        response.raise_for_status()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return dest


def parse_stock_list(path: Path) -> JpxStockList:
    """銘柄一覧の Excel を読んで正規化する.

    Raises:
        ValueError: 必要な列がない、基準日が 1 つに定まらない、対象銘柄が 0 件のいずれか
    """
    frame = pd.read_excel(path, dtype=str)

    missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"JPX 銘柄一覧に必要な列がありません: {missing}")

    base_date = _parse_base_date(frame)

    target = frame[frame["市場・商品区分"].isin(MARKET_SEGMENT_BY_JPX)]
    if target.empty:
        raise ValueError(f"対象の市場区分の銘柄が 1 件もありません: {path}")

    # to_dict の戻りは dict[Hashable, Any] 扱いになる。列名は必ず str なので絞る。
    records = cast(list[dict[str, Any]], target.to_dict("records"))
    stocks = [_build_stock(record) for record in records]
    logger.info("基準日 %s / 対象 %d 銘柄 (全 %d 行)", base_date, len(stocks), len(frame))
    return JpxStockList(base_date=base_date, stocks=stocks)


def fetch_stock_list(data_dir: Path) -> tuple[Path, JpxStockList]:
    """ダウンロードして基準日ごとのファイル名で保存し、正規化した内容を返す.

    保存先は ``<data_dir>/jpx/stock_list/data_j_YYYYMMDD.xls``。
    基準日はダウンロードするまで分からないため、いったん一時ファイルに落としてから移す。
    """
    save_dir = data_dir / "jpx" / "stock_list"
    save_dir.mkdir(parents=True, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(dir=save_dir, suffix=".xls.part", delete=False)
    handle.close()
    tmp_path = Path(handle.name)

    try:
        download_stock_list(tmp_path)
        data = parse_stock_list(tmp_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    dest = save_dir / f"data_j_{data.base_date:%Y%m%d}.xls"
    tmp_path.replace(dest)
    # 一時ファイルは 0600 で作られる。生データなので通常のファイル権限に戻す。
    dest.chmod(0o644)
    logger.info("保存: %s", dest)
    return dest, data


def _parse_base_date(frame: pd.DataFrame) -> date:
    """「日付」列から基準日を取る。JPX は月末時点のデータを遅れて公開するため取得日とは違う."""
    values = sorted({str(value).strip() for value in frame["日付"].dropna()})
    if len(values) != 1:
        raise ValueError(f"基準日が 1 つに定まりません: {values}")
    return datetime.strptime(values[0], "%Y%m%d").date()


def _build_stock(record: dict[str, Any]) -> JpxStock:
    code = _required(record, "コード")
    return JpxStock(
        code=code,
        name=_required(record, "銘柄名", code),
        market_segment=MARKET_SEGMENT_BY_JPX[_required(record, "市場・商品区分", code)],
        industry33_code=_zero_padded(record, "33業種コード", 4, code),
        industry33_name=_required(record, "33業種区分", code),
        industry17_code=_zero_padded(record, "17業種コード", 2, code),
        industry17_name=_required(record, "17業種区分", code),
        topix_scale_code=_optional(record, "規模コード"),
        topix_scale_name=_optional(record, "規模区分"),
    )


def _optional(record: dict[str, Any], column: str) -> str | None:
    """空欄と "-" を None にする."""
    value = record.get(column)
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return None if text in _MISSING_MARKS else text


def _required(record: dict[str, Any], column: str, code: str | None = None) -> str:
    text = _optional(record, column)
    if text is None:
        raise ValueError(f"{column} が空です (コード: {code or record.get('コード')})")
    return text


def _zero_padded(record: dict[str, Any], column: str, width: int, code: str) -> str:
    """業種コードを 0 埋めする.

    Excel が数値セルで持っているため先頭の 0 が落ちる。33 業種の "0050" が "50" になる。
    """
    text = _required(record, column, code)
    if not text.isdigit():
        raise ValueError(f"{column} が数字ではありません: {text!r} (コード: {code})")
    if len(text) > width:
        raise ValueError(f"{column} が {width} 桁を超えています: {text!r} (コード: {code})")
    return text.zfill(width)
