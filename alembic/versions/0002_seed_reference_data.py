"""seed reference data

Reference data the app can't work without: exchanges, timeframes, alert types. Created
by a migration, edited from the admin panel afterwards.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


exchanges = sa.table(
    "exchanges",
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("is_active", sa.Boolean),
    sa.column("supports_testnet", sa.Boolean),
    sa.column("sort_order", sa.Integer),
)

timeframes = sa.table(
    "timeframes",
    sa.column("code", sa.String),
    sa.column("label", sa.String),
    sa.column("seconds", sa.Integer),
    sa.column("is_active", sa.Boolean),
    sa.column("sort_order", sa.Integer),
)

alert_types = sa.table(
    "alert_types",
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("description", sa.Text),
    sa.column("is_active", sa.Boolean),
    sa.column("sort_order", sa.Integer),
)

EXCHANGE_ROWS = [
    {"code": "bybit", "name": "Bybit", "is_active": True, "supports_testnet": True, "sort_order": 10},
    {"code": "binance", "name": "Binance", "is_active": True, "supports_testnet": True, "sort_order": 20},
]

# The set from the spec: 1m / 5m / 15m / 1h / 4h / 1d.
TIMEFRAME_ROWS = [
    {"code": "1m", "label": "1 минута", "seconds": 60, "is_active": True, "sort_order": 10},
    {"code": "5m", "label": "5 минут", "seconds": 300, "is_active": True, "sort_order": 20},
    {"code": "15m", "label": "15 минут", "seconds": 900, "is_active": True, "sort_order": 30},
    {"code": "1h", "label": "1 час", "seconds": 3600, "is_active": True, "sort_order": 40},
    {"code": "4h", "label": "4 часа", "seconds": 14400, "is_active": True, "sort_order": 50},
    {"code": "1d", "label": "1 день", "seconds": 86400, "is_active": True, "sort_order": 60},
]

ALERT_TYPE_ROWS = [
    {
        "code": "price_above",
        "name": "Цена выше уровня",
        "description": "Срабатывает, когда цена поднимается выше заданного значения.",
        "is_active": True,
        "sort_order": 10,
    },
    {
        "code": "price_below",
        "name": "Цена ниже уровня",
        "description": "Срабатывает, когда цена опускается ниже заданного значения.",
        "is_active": True,
        "sort_order": 20,
    },
    {
        "code": "pct_change",
        "name": "Изменение в процентах",
        "description": "Срабатывает при изменении цены на заданный процент за выбранный период.",
        "is_active": True,
        "sort_order": 30,
    },
    {
        "code": "rsi",
        "name": "Уровень RSI",
        "description": "Срабатывает, когда RSI пересекает заданный порог сверху или снизу.",
        "is_active": True,
        "sort_order": 40,
    },
]


def upgrade() -> None:
    op.bulk_insert(exchanges, EXCHANGE_ROWS)
    op.bulk_insert(timeframes, TIMEFRAME_ROWS)
    op.bulk_insert(alert_types, ALERT_TYPE_ROWS)


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM alert_types WHERE code IN :codes").bindparams(
            sa.bindparam("codes", [r["code"] for r in ALERT_TYPE_ROWS], expanding=True)
        )
    )
    op.execute(
        sa.text("DELETE FROM timeframes WHERE code IN :codes").bindparams(
            sa.bindparam("codes", [r["code"] for r in TIMEFRAME_ROWS], expanding=True)
        )
    )
    op.execute(
        sa.text("DELETE FROM exchanges WHERE code IN :codes").bindparams(
            sa.bindparam("codes", [r["code"] for r in EXCHANGE_ROWS], expanding=True)
        )
    )
