"""bot order close price

Цена выхода из позиции. Результат сделки хранился, а по какой цене она
закрылась — нет, и в таблице ордеров это было видно только по журналу.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'bot_orders',
        sa.Column('close_price', sa.Numeric(precision=36, scale=18), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('bot_orders', 'close_price')
