"""EDINET から有価証券報告書とその訂正報告書を取得する.

このモジュールは DB を知らない。API の呼び出しと正規化だけを行い、結果を dataclass で返す。

書類一覧は提出日単位でしか引けない。過去に遡るには日付を 1 日ずつ舐めるしかない。
"""

import logging
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EDINET_API_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"

DOC_TYPE_ANNUAL_REPORT = "120"
"""有価証券報告書."""

DOC_TYPE_AMENDED_ANNUAL_REPORT = "130"
"""訂正有価証券報告書. 訂正された数値を拾うために有報と一緒に取る."""

TARGET_DOC_TYPES = frozenset({DOC_TYPE_ANNUAL_REPORT, DOC_TYPE_AMENDED_ANNUAL_REPORT})
"""取り込む書類種別。半期報告書 (160/170) が要るようになったらここに足す."""

REQUEST_INTERVAL = 0.25
"""連続してリクエストするときに空ける秒数。EDINET に負荷をかけないため."""

_LIST_TYPE_WITH_DOCUMENTS = 2
"""documents.json の type。2 でメタデータと書類一覧の両方が返る."""

_DOCUMENT_TYPE_XBRL_ZIP = 1
"""documents/{docID} の type。1 で XBRL 一式の ZIP が返る."""


class EdinetApiError(RuntimeError):
    """EDINET API がエラーを返した."""


@dataclass(frozen=True, slots=True)
class EdinetDocumentMeta:
    """書類 1 件のメタデータ。フィールド名は edinet_documents の列名に合わせてある."""

    doc_id: str
    edinet_code: str
    sec_code: str
    code: str
    doc_type_code: str
    parent_doc_id: str | None
    submit_date: date
    submitted_at: datetime | None
    period_end: date | None
    filer_name: str
    doc_description: str
    has_xbrl: bool
    is_withdrawn: bool


def fetch_document_list(
    target_date: date, api_key: str, timeout: float = 30.0
) -> list[EdinetDocumentMeta]:
    """ある提出日の書類一覧を引き、対象の書類種別だけを返す.

    土日祝も呼べる。提出が無い日は空のリストが返る。

    Raises:
        EdinetApiError: API がエラーステータスを返した場合
        httpx.HTTPError: 通信に失敗した場合
    """
    params: dict[str, str | int] = {
        "date": target_date.isoformat(),
        "type": _LIST_TYPE_WITH_DOCUMENTS,
    }
    with httpx.Client(timeout=timeout, headers=_auth_headers(api_key)) as client:
        response = client.get(f"{EDINET_API_BASE_URL}/documents.json", params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

    _raise_for_api_error(payload, target_date)

    results: list[dict[str, Any]] = payload.get("results") or []
    documents = [meta for record in results if (meta := _build_meta(record)) is not None]
    logger.debug("%s: 全 %d 件のうち対象 %d 件", target_date, len(results), len(documents))
    return documents


def download_document(doc_id: str, dest: Path, api_key: str, timeout: float = 120.0) -> Path:
    """XBRL 一式の ZIP をダウンロードして dest に保存する.

    途中で落ちても欠けたファイルが残らないよう、一時ファイルに書いてから移す。

    Raises:
        httpx.HTTPError: 通信に失敗した場合
    """
    params: dict[str, str | int] = {"type": _DOCUMENT_TYPE_XBRL_ZIP}

    dest.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".zip.part", delete=False)
    handle.close()
    tmp_path = Path(handle.name)

    try:
        with httpx.Client(timeout=timeout, headers=_auth_headers(api_key)) as client:
            with client.stream(
                "GET", f"{EDINET_API_BASE_URL}/documents/{doc_id}", params=params
            ) as response:
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


def document_path(data_dir: Path, doc_id: str, submit_date: date) -> Path:
    """ZIP の保存先を返す. ``<data_dir>/edinet/YYYYMMDD/{docID}.zip``.

    この配置は前身の findocgen が使っていたもので、既存の 6800 件をそのまま使う。
    """
    return data_dir / "edinet" / f"{submit_date:%Y%m%d}" / f"{doc_id}.zip"


def to_stock_code(sec_code: str) -> str:
    """EDINET の証券コードを JPX の銘柄コードに変換する.

    EDINET は 5 桁で末尾を 0 埋めする。``13010`` は JPX の ``1301``、``130A0`` は ``130A``。
    末尾が 0 でない 5 桁はそのまま返す。優先株のような枝番付きのコードがこの形になる。
    """
    if len(sec_code) == 5 and sec_code.endswith("0"):
        return sec_code[:4]
    return sec_code


def _auth_headers(api_key: str) -> dict[str, str]:
    """API キーはヘッダーで送る.

    クエリパラメータでも通るが、それだと httpx のログに URL ごと平文で残る。cron の
    ログに鍵が流れるため使わない。
    """
    return {"Ocp-Apim-Subscription-Key": api_key}


def _raise_for_api_error(payload: dict[str, Any], target_date: date) -> None:
    """EDINET はエラーでも HTTP 200 を返す。本文を見ないと失敗に気づけない.

    キーが無効なときは metadata ごと無く、代わりにトップレベルの StatusCode に 401 が入る。
    これを見落とすと「対象 0 件」として静かに完走してしまう。
    """
    status_code = payload.get("StatusCode")
    if status_code is not None:
        message = payload.get("message", "")
        raise EdinetApiError(
            f"EDINET API がエラーを返しました ({target_date}): {status_code} {message}"
        )

    metadata: dict[str, Any] = payload.get("metadata") or {}
    status = str(metadata.get("status", ""))
    if status != "200":
        message = metadata.get("message", "")
        raise EdinetApiError(
            f"EDINET API が想定外の応答を返しました ({target_date}): {status} {message}"
        )


def _build_meta(record: dict[str, Any]) -> EdinetDocumentMeta | None:
    """API のレコードを正規化する. 対象外なら None を返す."""
    if record.get("docTypeCode") not in TARGET_DOC_TYPES:
        return None

    # 証券コードが無いのは非上場の提出者。投資判断の対象にならない。
    sec_code = _text(record.get("secCode"))
    if sec_code is None:
        return None

    doc_id = _text(record.get("docID"))
    edinet_code = _text(record.get("edinetCode"))
    submitted_at = _parse_datetime(record.get("submitDateTime"))
    if doc_id is None or edinet_code is None or submitted_at is None:
        raise ValueError(f"書類のメタデータが欠けています: {record}")

    return EdinetDocumentMeta(
        doc_id=doc_id,
        edinet_code=edinet_code,
        sec_code=sec_code,
        code=to_stock_code(sec_code),
        doc_type_code=str(record["docTypeCode"]),
        parent_doc_id=_text(record.get("parentDocID")),
        submit_date=submitted_at.date(),
        submitted_at=submitted_at,
        period_end=_parse_date(record.get("periodEnd")),
        filer_name=_text(record.get("filerName")) or "",
        doc_description=_text(record.get("docDescription")) or "",
        has_xbrl=_flag(record.get("xbrlFlag")),
        # 0 は通常、1 は取り下げた書類、2 は取り下げられた書類。0 以外は本文を取りに行かない。
        is_withdrawn=str(record.get("withdrawalStatus", "0")) != "0",
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flag(value: Any) -> bool:
    """EDINET のフラグは文字列の "1" / "0" で来る."""
    return str(value) == "1"


def _parse_date(value: Any) -> date | None:
    text = _text(value)
    return None if text is None else datetime.strptime(text, "%Y-%m-%d").date()


def _parse_datetime(value: Any) -> datetime | None:
    """submitDateTime は "YYYY-MM-DD HH:MM" で来る. 秒もタイムゾーンも無い."""
    text = _text(value)
    return None if text is None else datetime.strptime(text, "%Y-%m-%d %H:%M")
