"""seed default signal rule

Default rule: EMA crossover with an RSI filter on the hourly timeframe. Shared by
everyone (user_id is empty) and not tied to a pair - it is evaluated against users'
watchlists.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RULE_NAME = "EMA-кроссовер с фильтром RSI"

DESCRIPTION = (
    "Покупка, когда быстрая EMA пересекает медленную снизу вверх и RSI "
    "не в зоне перекупленности. Продажа — зеркально. Сигнал выдаётся "
    "только по закрытой свече."
)

CONFIG = (
    '{"ema_fast": 9, "ema_slow": 21, "rsi_period": 14, '
    '"rsi_overbought": 70, "rsi_oversold": 30}'
)


def upgrade() -> None:
    # Look the timeframe up by code: reference-table ids depend on insertion
    # order in the previous migration.
    op.execute(
        sa.text(
            """
            INSERT INTO signal_rules
                (user_id, name, description, market_id, timeframe_id,
                 config, evaluation_horizon_minutes, is_active,
                 created_at, updated_at)
            SELECT NULL, :name, :description, NULL, timeframes.id,
                   CAST(:config AS jsonb), 1440, true, now(), now()
            FROM timeframes
            WHERE timeframes.code = '1h'
            """
        ).bindparams(name=RULE_NAME, description=DESCRIPTION, config=CONFIG)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM signal_rules WHERE name = :name").bindparams(name=RULE_NAME)
    )
