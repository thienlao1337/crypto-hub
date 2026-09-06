"""strategy last signal watermark

Отметка «сигналы до этого номера уже рассмотрены». Без неё каждый проход
фонового процесса разбирал те же сигналы заново и писал в журнал те же
отказы: за полчаса набегало три десятка одинаковых строк.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Без внешнего ключа намеренно: это водяной знак, а не ссылка на
    # запись. Удаление старого сигнала не должно обнулять отметку и
    # заставлять стратегию всё переосмысливать.
    op.add_column('strategies', sa.Column('last_signal_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('strategies', 'last_signal_id')
