"""市場指数の行を ticks から外す.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-12

TOPIX (998405) の 638 行が残っていた。findocgen から移したぶんで ``adjusted_close`` は全行
NULL、期間は 2024-01-04 から 2026-08-14 で止まっていた。kabu-app は指数を一度も取っていない。

続きが取れないので消す。Yahoo は指数の株価時系列ページを返さない。``.T`` も ``.O`` も
HTTP 200 で戻るが ``histories`` が空、``pager`` が null で、本文に「株価時系列」の語も無い。
期間指定や ``styl`` を変えても同じだった。日経平均 (998407) も取れない。

途中で止まった指数はベンチマークに使えない。残しても「あるのに使えない列」になる。
要るようになったら JPX や日経が公開するデータを別の経路で取ること。

データの削除はこの migration に含めない。適用済みの環境では手で消すこと。

    DELETE FROM ticks WHERE code = '998405';
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW = "JPX 銘柄コード。4 桁英数字が基本。優先株・種類株は 5 桁。市場指数は入れない"
_OLD = "JPX 銘柄コード。市場指数は 998405 のような 6 桁"


def upgrade() -> None:
    op.alter_column(
        "ticks",
        "code",
        existing_type=sa.VARCHAR(length=8),
        comment=_NEW,
        existing_comment=_OLD,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "ticks",
        "code",
        existing_type=sa.VARCHAR(length=8),
        comment=_OLD,
        existing_comment=_NEW,
        existing_nullable=False,
    )
