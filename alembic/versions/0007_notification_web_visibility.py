"""notification web visibility

Признак показа уведомления в ленте панели. Нужен, чтобы каналы «панель»
и «Telegram» отключались независимо: таблица уведомлений одновременно и
лента, и очередь отправки, поэтому «только в Telegram» без отдельного
флага выразить нечем.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default обязателен: в таблице уже есть строки, и NOT NULL
    # без значения по умолчанию свалил бы миграцию на боевой базе.
    # Старые уведомления в ленте показывались все — значит, true.
    op.add_column(
        'notifications',
        sa.Column('show_web', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Значение по умолчанию нужно было только для заполнения старых строк.
    op.alter_column('notifications', 'show_web', server_default=None)


def downgrade() -> None:
    op.drop_column('notifications', 'show_web')
