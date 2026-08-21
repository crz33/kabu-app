"""findocgen から吐いた CSV を tdnet_disclosures に入れる.

``import_findocgen_tdnet.sh`` から呼ばれる。1 度だけ実行する想定だが、doc_id が既にある行は
飛ばすので何度流しても増えない。

移した行は sec_code / markets / xbrl_file が NULL になる。findocgen が持っていない列のため。
downloaded_at には findocgen の updated を入れる。実体はもう置き場にあるので、
落とし直しに行かせない。
"""

import csv
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from kabu_app.config import get_settings
from kabu_app.db import create_session_factory, session_scope
from kabu_app.models import TdnetDisclosure

_CHUNK_SIZE = 1000


def read_rows(path: Path) -> list[dict[str, Any]]:
    """CSV を tdnet_disclosures の列に合わせて読む."""
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.reader(handle):
            doc_id, submit_date, submit_time, code, filer_name, doc_desc, updated = record
            rows.append(
                {
                    "doc_id": doc_id,
                    "disclosed_date": date.fromisoformat(submit_date),
                    "disclosed_time": time.fromisoformat(submit_time),
                    "sec_code": None,
                    "code": code,
                    "company_name": filer_name,
                    "title": doc_desc,
                    "markets": None,
                    "is_amendment": "訂正" in doc_desc,
                    "xbrl_file": None,
                    "downloaded_at": datetime.fromisoformat(updated),
                }
            )
    return rows


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: import_findocgen_tdnet.py <csv>", file=sys.stderr)
        return 2

    rows = read_rows(Path(sys.argv[1]))
    print(f"CSV から {len(rows)} 行を読んだ")

    count = select(func.count()).select_from(TdnetDisclosure)
    factory = create_session_factory(get_settings().database_url)
    with session_scope(factory) as session:
        before = session.execute(count).scalar_one()
        for start in range(0, len(rows), _CHUNK_SIZE):
            statement = insert(TdnetDisclosure).values(rows[start : start + _CHUNK_SIZE])
            session.execute(
                statement.on_conflict_do_nothing(index_elements=[TdnetDisclosure.doc_id])
            )
        after = session.execute(count).scalar_one()

    print(f"{after - before} 行を入れた (doc_id が既にあった行は飛ばした)。合計 {after} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
