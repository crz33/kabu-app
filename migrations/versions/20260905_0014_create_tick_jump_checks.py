"""調整後終値の飛びを確認済みとして記録する表を作る.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-05

``kabu fetch ticks --only-jumps`` は調整後終値が前日から大きく飛んだ銘柄を洗い出して
取り直す。狙いは分割の調整漏れで、この判定は本物の急騰・急落と区別がつかない。低位株は
1 円刻みで動くので 3 円 → 1 円が普通に起きる。値幅制限も 100 円未満なら ±30 円あるため、
19 円の銘柄は 1 日で 2.6 倍まで動ける。売買が成立しない日が続いた銘柄は、値が付いた日に
制限値幅を超えて飛ぶ。どれも本物の値動きなので、取り直しても同じ値が返ってくる。

そのため対象が減らなくなる。2026 年 9 月の時点で 11 銘柄 12 箇所が残り、毎晩 14 分かけて
2024 年以降を取り直しては同じ値を受け取っていた。

取り直しても消えなかった飛びをここに入れて、次からの判定で除外する。判定は全期間を見た
ままにする。期間で切ると、分割の全期間取り直しが途中で失敗した銘柄を拾い損ねる。240 万行
を走査しても 3.8 秒で、``pk_ticks`` が (code, date) 順なのでソートも要らない。

飛びの位置 (銘柄, 日) だけを持ち、そのときの値は持たない。分割が起きるたびに Yahoo は過去
の調整後終値を書き換えるので、値で照合すると分割のたびに記録が無効になる。

``ticks`` への外部キーは張らない。上場廃止した銘柄の記録も残すため。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tick_jump_checks",
        sa.Column(
            "code",
            sa.String(length=8),
            nullable=False,
            comment="JPX 銘柄コード。ticks に合わせて外部キーは張らない",
        ),
        sa.Column(
            "date",
            sa.Date(),
            nullable=False,
            comment="調整後終値が前日から飛んだ日",
        ),
        sa.Column(
            "note",
            sa.Text(),
            nullable=True,
            comment="除外した理由。自動記録では空。「低位株の 1 円刻み」などを後から手で書く",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="行の作成日時。この飛びを最初に確認した日時にあたる",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="行の更新日時。同じ飛びを再確認するたびに更新される",
        ),
        sa.PrimaryKeyConstraint("code", "date", name="pk_tick_jump_checks"),
        comment="取り直しても消えなかった調整後終値の飛び。次の判定から除外する",
    )


def downgrade() -> None:
    op.drop_table("tick_jump_checks")
